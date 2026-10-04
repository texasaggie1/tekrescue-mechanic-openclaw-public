"""Supervisor state file.

Tracks the running state of Mechanic across launchd-scheduled invocations:
last-run timestamps, the consecutive-failure counter, the pause flag, and
pointers to the current known-good snapshots. State is read at the start of
every updater and supervisor invocation and written atomically
(write-to-tempfile-then-rename) so concurrent launchd processes never see a
half-written file.

The on-disk format is a single JSON object per target. OpenClaw keeps the
original path, ~/Library/Application Support/tekrescue-mechanic/state/
supervisor_state.json, so existing installs carry their history forward;
every other target uses supervisor_state.<target>.json next to it. Pause
and the failure counter are per target: a Hermes bad night never stops the
OpenClaw nightly, and the other way round.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import RUNTIME_STATE_DIR


STATE_FILE = RUNTIME_STATE_DIR / "supervisor_state.json"


def state_path_for(target: str) -> Path:
    """The state file for a target. OpenClaw keeps the pre-v0.2 path."""
    if target == "openclaw":
        return STATE_FILE
    return RUNTIME_STATE_DIR / f"supervisor_state.{target}.json"

_LOG = logging.getLogger(__name__)


class StateError(Exception):
    """Raised when the supervisor state file is unreadable or malformed."""


@dataclass(frozen=True)
class SupervisorState:
    """The persisted supervisor state.

    All timestamp fields are ISO 8601 strings in UTC, or None if the event has
    never happened. Transitions are produced by the helper functions in this
    module, which return new SupervisorState instances and save them.
    """

    last_run: Optional[str] = None
    last_success: Optional[str] = None
    consecutive_failures: int = 0
    paused: bool = False
    pause_reason: Optional[str] = None
    paused_at: Optional[str] = None
    last_known_good_snapshot_id: Optional[str] = None
    first_known_good_captured: bool = False


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(path: Path | None = None, *, target: str = "openclaw") -> SupervisorState:
    """Read a target's supervisor state file.

    Returns a default SupervisorState if the file does not exist yet (first
    run). Raises StateError if the file exists but is unreadable or contains
    a non-object payload. `path` overrides the location (tests).
    """
    target = path or state_path_for(target)
    if not target.exists():
        return SupervisorState()

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"Could not read state file {target}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(f"State file {target} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise StateError(f"State file {target} is not a JSON object.")

    fields = SupervisorState.__dataclass_fields__.keys()
    known = {k: data[k] for k in fields if k in data}
    return SupervisorState(**known)


def save_state(
    state: SupervisorState, path: Path | None = None, *, target: str = "openclaw"
) -> None:
    """Persist a target's supervisor state atomically.

    Writes to a temporary file in the same directory, then renames over the
    destination. os.replace is atomic on POSIX, so a concurrent reader sees
    either the previous contents or the new contents, never a partial write.
    """
    target = path or state_path_for(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"

    fd, tmp_path = tempfile.mkstemp(
        prefix=".supervisor_state.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def record_run_start(state: SupervisorState) -> SupervisorState:
    """Stamp last_run with the current time. Called by the updater on entry."""
    return replace(state, last_run=now_iso())


def record_success(
    state: SupervisorState,
    *,
    snapshot_id: Optional[str] = None,
) -> SupervisorState:
    """Reset failure counter, record last_success, optionally pin last-known-good."""
    return replace(
        state,
        last_success=now_iso(),
        consecutive_failures=0,
        last_known_good_snapshot_id=snapshot_id or state.last_known_good_snapshot_id,
    )


def record_failure(
    state: SupervisorState,
    *,
    max_consecutive_failures: int,
) -> SupervisorState:
    """Increment the failure counter. Auto-pause if the threshold is reached."""
    new_count = state.consecutive_failures + 1
    if new_count >= max_consecutive_failures:
        return _pause(
            replace(state, consecutive_failures=new_count),
            reason=f"{new_count} consecutive verify failures",
        )
    return replace(state, consecutive_failures=new_count)


def force_pause(state: SupervisorState, *, reason: str) -> SupervisorState:
    """Enter paused mode regardless of failure count. Used on rollback failure."""
    return _pause(state, reason=reason)


def resume(state: SupervisorState) -> SupervisorState:
    """Clear paused mode and reset the consecutive-failure counter.

    Called by `mechanic resume`. Per CLAUDE.md section 5: clears `paused`,
    `pause_reason`, `paused_at`, and resets `consecutive_failures` to 0.
    Leaves last_run, last_success, and snapshot pointers untouched.
    """
    return replace(
        state,
        paused=False,
        pause_reason=None,
        paused_at=None,
        consecutive_failures=0,
    )


def mark_first_known_good_captured(state: SupervisorState) -> SupervisorState:
    """Flip first_known_good_captured to True after a successful first snapshot."""
    return replace(state, first_known_good_captured=True)


def _pause(state: SupervisorState, *, reason: str) -> SupervisorState:
    if state.paused:
        _LOG.debug("already paused; keeping existing reason %r", state.pause_reason)
        return state
    return replace(
        state,
        paused=True,
        pause_reason=reason,
        paused_at=now_iso(),
    )
