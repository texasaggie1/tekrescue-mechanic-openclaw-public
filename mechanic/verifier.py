"""Health and update-availability probes for OpenClaw.

The verifier runs `openclaw --version` (or another configured probe command)
under a short timeout and reports whether OpenClaw is responsive. The nightly
updater uses the result to decide whether to keep the new state or roll back.

This module also owns the update-availability probe (`openclaw update status
--json`), shared by the supervisor heartbeat and, since v0.1.2, by the
nightly updater to decide whether running `openclaw update` is worth touching
the install at all.

The health check is intentionally thin. v0.1 verifies that OpenClaw launches
and exits cleanly; it does not exercise OpenClaw's full feature surface.
Deeper verification can be added once we learn what failure modes show up in
the wild without overfitting to today's behaviour.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .config import Config, clean_subprocess_env


DEFAULT_TIMEOUT_SECONDS = 20
UPDATE_STATUS_TIMEOUT_SECONDS = 30
_PROBE_ARGS: tuple[str, ...] = ("--version",)

_VERSION_PATTERN = re.compile(r"(?<![.\d])(\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)*)")

_TAIL_BYTES = 500

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerifyResult:
    """Structured outcome of a verifier probe.

    The updater branches on `healthy`. The remaining fields exist so the
    morning report and the log have something useful to show when verification
    fails (the duration, the exit status, the captured tail of OpenClaw's
    own output).
    """

    healthy: bool
    version: Optional[str]
    reason: Optional[str]
    exit_code: Optional[int]
    duration_ms: int
    stdout_tail: str
    stderr_tail: str

    def short_summary(self) -> str:
        """One-line human-readable summary, safe to log or include in reports."""
        if self.healthy:
            version = self.version or "unknown version"
            return f"healthy ({version}, exit 0, {self.duration_ms} ms)"
        reason = self.reason or "unknown reason"
        return f"unhealthy ({reason})"


def verify_openclaw(
    config: Config,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    probe_args: Sequence[str] = _PROBE_ARGS,
) -> VerifyResult:
    """Probe OpenClaw and return whether it is currently responsive.

    Thin wrapper over verify_binary for the OpenClaw target; kept so the
    existing call sites and docs stay true.
    """
    return verify_binary(
        config.openclaw_bin_path,
        label="OpenClaw",
        timeout_seconds=timeout_seconds,
        probe_args=probe_args,
    )


def verify_binary(
    bin_path: Path,
    *,
    label: str = "binary",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    probe_args: Sequence[str] = _PROBE_ARGS,
) -> VerifyResult:
    """Probe a target's executable and return whether it is responsive.

    Args:
        bin_path: The executable that gets invoked.
        label: Product name for messages (OpenClaw, Hermes).
        timeout_seconds: How long to wait before declaring the probe stuck.
        probe_args: The arguments to pass after the binary. Defaults to
            ('--version',), which is non-destructive and well-defined on
            most CLIs.

    Returns:
        A VerifyResult. `healthy` is True only if the binary existed and
        exited 0 within the timeout. `version` is whatever version-shaped
        token the output carried, or None.
    """
    if not bin_path.exists():
        return _result(
            healthy=False,
            reason=f"{label} binary not found at {bin_path}",
            exit_code=None,
            duration_ms=0,
        )
    if not bin_path.is_file():
        return _result(
            healthy=False,
            reason=f"{label} binary at {bin_path} is not a regular file",
            exit_code=None,
            duration_ms=0,
        )

    cmd = [str(bin_path), *probe_args]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean_subprocess_env(),
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return _result(
            healthy=False,
            reason=f"timed out after {timeout_seconds}s running {cmd!r}",
            exit_code=None,
            duration_ms=elapsed,
            stdout_tail=_tail((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, (bytes, bytearray)) else (exc.stdout or "")),
            stderr_tail=_tail((exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, (bytes, bytearray)) else (exc.stderr or "")),
        )
    except OSError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return _result(
            healthy=False,
            reason=f"could not invoke {bin_path}: {exc}",
            exit_code=None,
            duration_ms=elapsed,
        )

    duration_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        return _result(
            healthy=False,
            reason=f"exit code {proc.returncode}",
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            stdout_tail=_tail(proc.stdout),
            stderr_tail=_tail(proc.stderr),
        )

    version = extract_version(proc.stdout) or extract_version(proc.stderr)

    return _result(
        healthy=True,
        version=version,
        reason=None,
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        stdout_tail=_tail(proc.stdout),
        stderr_tail=_tail(proc.stderr),
    )


@dataclass(frozen=True)
class UpdateAvailability:
    """Result of an `openclaw update status --json` probe.

    `channel` is the update channel OpenClaw reports (stable, beta,
    extended-stable, dev) when the payload carries one, lowercased. None
    when the payload has no recognisable channel field; callers treat that
    as stable, OpenClaw's default.
    """

    available: bool
    latest_version: Optional[str]
    error: Optional[str]
    channel: Optional[str] = None


def check_for_updates(config: Config) -> UpdateAvailability:
    """Run `openclaw update status --json` for the configured OpenClaw."""
    return check_openclaw_updates(config.openclaw_bin_path)


def check_openclaw_updates(bin_path: Path) -> UpdateAvailability:
    """Run `openclaw update status --json` and parse availability.

    Read-only probe; returns in milliseconds and never touches the install.
    Errors (timeout, non-zero exit, non-JSON output) degrade into the
    `error` field rather than raising, so both the heartbeat and the
    nightly gate can report them without crashing.
    """
    cmd = [str(bin_path), "update", "status", "--json"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean_subprocess_env(),
            timeout=UPDATE_STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return UpdateAvailability(
            available=False, latest_version=None,
            error=f"`openclaw update status` timed out after {UPDATE_STATUS_TIMEOUT_SECONDS}s",
        )
    except OSError as exc:
        return UpdateAvailability(
            available=False, latest_version=None,
            error=f"could not invoke openclaw update status: {exc}",
        )

    if proc.returncode != 0:
        return UpdateAvailability(
            available=False, latest_version=None,
            error=f"openclaw update status exit {proc.returncode}: "
                  f"{(proc.stderr or proc.stdout).strip()[:200]}",
        )

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return UpdateAvailability(
            available=False, latest_version=None,
            error=f"openclaw update status emitted non-JSON: {exc}",
        )

    availability = payload.get("availability") or {}
    return UpdateAvailability(
        available=bool(availability.get("available")),
        latest_version=availability.get("latestVersion"),
        error=None,
        channel=_extract_channel(payload),
    )


def _extract_channel(payload: object) -> Optional[str]:
    """Best-effort read of the update channel from the status payload.

    OpenClaw's documentation says `update status --json` reports the
    channel but does not pin down the key, so this looks in the places it
    could plausibly live and accepts either a bare string or an object
    with a `name`/`channel` string. Anything else yields None.
    """
    if not isinstance(payload, dict):
        return None
    availability = payload.get("availability")
    update = payload.get("update")
    candidates = [
        payload.get("channel"),
        availability.get("channel") if isinstance(availability, dict) else None,
        update.get("channel") if isinstance(update, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("name") or candidate.get("channel")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return None


def extract_version(text: str) -> Optional[str]:
    """Pull a version-shaped token out of OpenClaw's probe output.

    Matches digits.digits(.digits)? with optional pre-release/build tail.
    Returns None if no plausible version is found. Used both inside
    verify_openclaw and by the updater when stamping snapshot metadata.
    """
    if not text:
        return None
    match = _VERSION_PATTERN.search(text)
    return match.group(1) if match else None


def _tail(text: str) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= _TAIL_BYTES:
        return text.rstrip()
    return encoded[-_TAIL_BYTES:].decode("utf-8", "replace").rstrip()


def _result(
    *,
    healthy: bool,
    reason: Optional[str] = None,
    version: Optional[str] = None,
    exit_code: Optional[int] = None,
    duration_ms: int = 0,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> VerifyResult:
    result = VerifyResult(
        healthy=healthy,
        version=version,
        reason=reason,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )
    if healthy:
        _LOG.info("verifier: %s", result.short_summary())
    else:
        _LOG.warning("verifier: %s", result.short_summary())
        if stderr_tail:
            _LOG.debug("verifier stderr tail: %s", stderr_tail)
    return result
