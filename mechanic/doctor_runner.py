"""Driver for `openclaw doctor --fix`.

Two modes:

* **Run mode** (the nightly path): subprocess.run of
  `openclaw doctor --fix --non-interactive`. The `--non-interactive` flag
  is an OpenClaw-side guarantee that doctor will not prompt; it applies
  only "safe migrations" and skips anything that would require an answer.
  We trust that guarantee and use subprocess.run with a single overall
  timeout. The exit code is the source of truth. KNOWN_PROMPTS is not
  consulted in v0.1 because there is nothing to match.

* **Capture mode** (one-shot, operator-driven, kept for v0.2): hands the
  doctor session to the operator via pexpect.interact while teeing
  stdout and stdin into an internal transcript. After doctor exits,
  candidate (prompt, response) pairs are extracted and offered to the
  operator for confirmation, then appended to KNOWN_PROMPTS. This sets us
  up to drop `--non-interactive` in v0.2 once we know which prompts are
  worth auto-answering.

Persistence: KNOWN_PROMPTS lives at
~/Library/Application Support/tekrescue-mechanic/state/known_prompts.json as a
JSON list of {"pattern": ..., "response": ...} objects. First match wins;
the file's order is the matching order, so put more specific patterns first.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config, RUNTIME_STATE_DIR, clean_subprocess_env


KNOWN_PROMPTS_FILE = RUNTIME_STATE_DIR / "known_prompts.json"

DEFAULT_RUN_TIMEOUT_SECONDS = 600
DEFAULT_PROMPT_TIMEOUT_SECONDS = 60

# Tell OpenClaw that something else (launchd) owns the gateway service, so
# doctor performs config repair only and never stops/starts/reinstalls the
# service itself. Supported by OpenClaw since 2026.4.25-beta.1.
#
# Why this is not optional (four outages in 2026-07 and 2026-09): `openclaw
# doctor --fix` stops the gateway to enter maintenance, then tries to
# restart it. On a host running EDR the restart aborts, because before
# mutating its LaunchAgent OpenClaw scans every /Library/LaunchDaemons/*.plist
# to prove no system daemon owns its label, and fails closed on any plist it
# cannot read. Elastic Defend keeps co.elastic.endpoint.plist at mode 644
# while blocking reads at the Endpoint Security layer, so OpenClaw's
# "unreadable, skip it" escape hatch (which tests permission bits via
# fs.access) never fires. Doctor then exits leaving the gateway STOPPED, and
# a `--version` probe still reports healthy because the CLI works fine with
# a dead gateway. Three of those outages started within one second of the
# 02:00 doctor step. With the policy set, doctor declines maintenance ("Stop
# the Gateway through its service owner") instead of stopping it, and never
# touches the plists, which also stops the EDR raising a case every night.
SERVICE_REPAIR_POLICY_ENV = "OPENCLAW_SERVICE_REPAIR_POLICY"
SERVICE_REPAIR_POLICY_EXTERNAL = "external"

_LOG = logging.getLogger(__name__)


class DoctorError(Exception):
    """Raised when doctor cannot be invoked or hits an unrecoverable state."""


class UnknownPromptAbort(DoctorError):
    """Raised when doctor emits a prompt Mechanic does not know how to answer.

    The updater catches this and rolls back. The carried prompt_text is
    surfaced to the notifier so the operator can extend KNOWN_PROMPTS.
    """

    def __init__(self, prompt_text: str, mode: str) -> None:
        super().__init__(
            f"Unknown prompt from openclaw doctor under PROMPT_MODE={mode}: "
            f"{prompt_text!r}"
        )
        self.prompt_text = prompt_text
        self.mode = mode


@dataclass(frozen=True)
class KnownPrompt:
    """One entry in KNOWN_PROMPTS: a regex pattern and the response to send."""

    pattern: str
    response: str


@dataclass
class DoctorResult:
    """Outcome of a doctor run.

    The updater consults `success` to decide whether to proceed to verify or
    roll back immediately. `transcript_tail` is included in the morning
    report on failure paths.
    """

    success: bool
    exit_code: Optional[int]
    matched_prompts: list[str] = field(default_factory=list)
    auto_yes_prompts: list[str] = field(default_factory=list)
    transcript_tail: str = ""
    reason: Optional[str] = None


def load_known_prompts(path: Path | None = None) -> list[KnownPrompt]:
    """Load KNOWN_PROMPTS from disk. Returns an empty list if the file is missing.

    Raises DoctorError on malformed JSON so the updater fails loudly rather
    than silently treating the file as empty.
    """
    target = path or KNOWN_PROMPTS_FILE
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DoctorError(f"Could not read {target}: {exc}") from exc
    if not isinstance(data, list):
        raise DoctorError(f"{target} must be a JSON list of prompt objects.")
    prompts: list[KnownPrompt] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or "pattern" not in entry or "response" not in entry:
            raise DoctorError(
                f"{target} entry {i} must have 'pattern' and 'response' keys."
            )
        prompts.append(
            KnownPrompt(pattern=str(entry["pattern"]), response=str(entry["response"]))
        )
    return prompts


def save_known_prompts(prompts: list[KnownPrompt], path: Path | None = None) -> None:
    """Persist KNOWN_PROMPTS atomically (tempfile + rename)."""
    target = path or KNOWN_PROMPTS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            [{"pattern": p.pattern, "response": p.response} for p in prompts],
            indent=2,
        )
        + "\n"
    )
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


def run_doctor(
    config: Config,
    *,
    prompts: Optional[list[KnownPrompt]] = None,
    run_timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS,
    prompt_timeout_seconds: int = DEFAULT_PROMPT_TIMEOUT_SECONDS,
) -> DoctorResult:
    """Run `openclaw doctor --fix --non-interactive`.

    OpenClaw's `--non-interactive` flag guarantees no prompts will appear
    (doctor only applies "safe migrations" in that mode). v0.1 takes that
    guarantee and uses subprocess.run with a single overall timeout; the
    pexpect-based prompt loop is reserved for v0.2 when we drop the flag.

    The `prompts` and `prompt_timeout_seconds` arguments are kept for
    forward compatibility but ignored in v0.1's non-interactive path.
    """
    del prompts, prompt_timeout_seconds  # reserved for v0.2 interactive path

    mode = config.prompt_mode
    cmd = [str(config.openclaw_bin_path), "doctor", "--fix", "--non-interactive"]
    _LOG.info(
        "doctor: running %s (PROMPT_MODE=%s, non-interactive, %s=%s)",
        cmd, mode, SERVICE_REPAIR_POLICY_ENV, SERVICE_REPAIR_POLICY_EXTERNAL,
    )

    doctor_env = clean_subprocess_env()
    doctor_env[SERVICE_REPAIR_POLICY_ENV] = SERVICE_REPAIR_POLICY_EXTERNAL

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=doctor_env,
            timeout=run_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _LOG.error("doctor: timed out after %ss", run_timeout_seconds)
        return DoctorResult(
            success=False,
            exit_code=None,
            transcript_tail=_tail(_decode(exc.stdout) + _decode(exc.stderr)),
            reason=f"doctor exceeded overall timeout of {run_timeout_seconds}s (after {elapsed_ms} ms)",
        )
    except OSError as exc:
        raise DoctorError(f"could not invoke {cmd[0]}: {exc}") from exc

    transcript_tail = _tail((proc.stdout or "") + (proc.stderr or ""))
    success = proc.returncode == 0
    return DoctorResult(
        success=success,
        exit_code=proc.returncode,
        transcript_tail=transcript_tail,
        reason=None if success else f"doctor exited with status {proc.returncode}",
    )


def _decode(payload: object) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        return payload
    return ""


def capture_prompts(
    config: Config,
    *,
    prompts_path: Path | None = None,
) -> list[KnownPrompt]:
    """Run doctor interactively; record the operator's prompt/response pairs.

    The operator sees a normal doctor session in their terminal. Mechanic
    tees stdin and stdout into a transcript, then after doctor exits offers
    each candidate (prompt, response) pair for confirmation and appends the
    confirmed entries to KNOWN_PROMPTS. Existing entries are preserved.
    """
    import pexpect

    cmd = [str(config.openclaw_bin_path), "doctor", "--fix"]
    print(f"Launching {' '.join(cmd)}. Answer prompts as you normally would.")
    print("When doctor exits, you will be asked to confirm each captured pair.\n")

    transcript: list[tuple[str, bytes]] = []

    def out_filter(buf: bytes) -> bytes:
        transcript.append(("out", buf))
        return buf

    def in_filter(buf: bytes) -> bytes:
        transcript.append(("in", buf))
        return buf

    child = pexpect.spawn(cmd[0], cmd[1:], echo=False)
    child.interact(input_filter=in_filter, output_filter=out_filter)
    child.close()

    pairs = _extract_pairs(transcript)
    if not pairs:
        print("\nNo prompt/response pairs detected. Nothing to save.")
        return load_known_prompts(prompts_path)

    print(f"\nDetected {len(pairs)} candidate prompt/response pair(s).")
    confirmed: list[KnownPrompt] = []
    for prompt_text, response in pairs:
        print()
        print(f"  prompt:   {prompt_text!r}")
        print(f"  response: {response!r}")
        ans = input("  Save this pair? [Y/n] ").strip().lower()
        if ans in ("", "y", "yes"):
            confirmed.append(
                KnownPrompt(pattern=re.escape(prompt_text), response=response)
            )

    if not confirmed:
        print("\nNo pairs confirmed. KNOWN_PROMPTS unchanged.")
        return load_known_prompts(prompts_path)

    existing = load_known_prompts(prompts_path)
    existing_keys = {(p.pattern, p.response) for p in existing}
    new_entries = [p for p in confirmed if (p.pattern, p.response) not in existing_keys]
    merged = existing + new_entries
    save_known_prompts(merged, prompts_path)
    print(
        f"\nSaved {len(new_entries)} new prompt(s). "
        f"KNOWN_PROMPTS now has {len(merged)} entries."
    )
    return merged


def _extract_pairs(transcript: list[tuple[str, bytes]]) -> list[tuple[str, str]]:
    """Walk the transcript and pair each operator input with the prompt above it."""
    pairs: list[tuple[str, str]] = []
    out_buffer = bytearray()
    current_input = bytearray()
    for direction, chunk in transcript:
        if direction == "out":
            if current_input:
                response = current_input.decode("utf-8", "replace").rstrip("\r\n")
                if response:
                    prompt_text = _last_nonblank_line(
                        out_buffer.decode("utf-8", "replace")
                    )
                    if prompt_text:
                        pairs.append((prompt_text, response))
                current_input.clear()
                out_buffer.clear()
            out_buffer.extend(chunk)
        else:
            current_input.extend(chunk)
    if current_input:
        response = current_input.decode("utf-8", "replace").rstrip("\r\n")
        if response:
            prompt_text = _last_nonblank_line(out_buffer.decode("utf-8", "replace"))
            if prompt_text:
                pairs.append((prompt_text, response))
    return pairs


def _last_nonblank_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return text.strip()


def _tail(text: str, max_chars: int = 1500) -> str:
    if len(text) <= max_chars:
        return text.strip()
    return text[-max_chars:].strip()
