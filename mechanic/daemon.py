"""Gateway daemon control for the supervised products.

Both OpenClaw (`ai.openclaw.gateway`) and Hermes (`ai.hermes.gateway`) run
their gateway as a user LaunchAgent that writes to the product's data
directory continuously. Restoring a snapshot is only safe with the daemon
stopped (otherwise the daemon recreates files in the data dir during the
untar, leaving a corrupted state).

This module wraps `launchctl unload/load` with a wait-until-down poll so the
restore flow knows when it is safe to mutate the data dir, and it reads
`launchctl print-disabled` so Mechanic can tell when the OPERATOR has
switched a gateway off on purpose.

That last part is a scar (SESSIONS.md 2026-10-04): a `launchctl bootout` of
the OpenClaw gateway lasted exactly one supervisor tick. The next read-only
probe (`openclaw --version`, `openclaw update status --json`) had OpenClaw
bring its own gateway back. Mechanic now treats "disabled by the operator"
as "do not run this product's CLI at all".

Labels and plist paths are user-level (not root) and follow each product's
standard install; the plist directory is `~/Library/LaunchAgents`.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional


OPENCLAW_DAEMON_LABEL = "ai.openclaw.gateway"
HERMES_DAEMON_LABEL = "ai.hermes.gateway"
DAEMON_LABELS = {"openclaw": OPENCLAW_DAEMON_LABEL, "hermes": HERMES_DAEMON_LABEL}

LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
OPENCLAW_DAEMON_PLIST = LAUNCHAGENTS_DIR / f"{OPENCLAW_DAEMON_LABEL}.plist"

_LOG = logging.getLogger(__name__)

_DISABLED_LINE = re.compile(r'"(?P<label>[^"]+)"\s*=>\s*(?P<state>disabled|enabled)')


class DaemonControlError(Exception):
    """Raised when stopping or starting a gateway daemon fails."""


def label_for(target: str) -> str:
    """The launchd label of a target's gateway."""
    try:
        return DAEMON_LABELS[target]
    except KeyError:
        raise DaemonControlError(f"no gateway label known for target {target!r}")


def plist_path_for(label: str) -> Path:
    """Where a user LaunchAgent with `label` keeps its plist."""
    return LAUNCHAGENTS_DIR / f"{label}.plist"


def is_loaded(label: str = OPENCLAW_DAEMON_LABEL) -> bool:
    """Return True if launchctl knows about the daemon under `label`."""
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def is_disabled(label: str) -> Optional[bool]:
    """Whether the operator has `launchctl disable`d this user service.

    Reads `launchctl print-disabled gui/<uid>`, whose output lists every
    service with an explicit enabled/disabled override as
    `"label" => disabled`. Returns True when the label is listed as
    disabled, False when it is listed as enabled or not listed at all, and
    None when launchctl is unavailable or the query failed (a non-macOS
    test box, or a launchd that refused the query). Callers treat None as
    "unknown, assume not disabled" and say so.
    """
    try:
        result = subprocess.run(
            ["launchctl", "print-disabled", f"gui/{os.getuid()}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return parse_disabled(result.stdout).get(label, False)


def parse_disabled(output: str) -> dict[str, bool]:
    """Parse `launchctl print-disabled` output into {label: disabled?}."""
    found: dict[str, bool] = {}
    for match in _DISABLED_LINE.finditer(output or ""):
        found[match.group("label")] = match.group("state") == "disabled"
    return found


def stop(
    *,
    label: str = OPENCLAW_DAEMON_LABEL,
    plist_path: Optional[Path] = None,
    wait_timeout_seconds: int = 30,
) -> None:
    """Unload the daemon and poll until it is no longer listed.

    No-op if the daemon was not loaded to begin with.
    """
    plist_path = plist_path or plist_path_for(label)
    if not is_loaded(label):
        _LOG.info("daemon: %s already not loaded", label)
        return

    if not plist_path.exists():
        raise DaemonControlError(
            f"Cannot stop {label}: plist {plist_path} does not exist."
        )

    _LOG.info("daemon: unloading %s", label)
    try:
        result = subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise DaemonControlError(f"launchctl unload failed: {exc}") from exc
    if result.returncode != 0:
        raise DaemonControlError(
            f"launchctl unload {plist_path} exit {result.returncode}: "
            f"{result.stderr.strip()}"
        )

    deadline = time.monotonic() + wait_timeout_seconds
    while time.monotonic() < deadline:
        if not is_loaded(label):
            _LOG.info("daemon: %s confirmed down", label)
            return
        time.sleep(0.5)
    raise DaemonControlError(
        f"{label} did not drop out of launchctl list within {wait_timeout_seconds}s."
    )


def start(
    *,
    label: str = OPENCLAW_DAEMON_LABEL,
    plist_path: Optional[Path] = None,
    wait_timeout_seconds: int = 30,
) -> None:
    """Load the daemon and poll until it is listed.

    No-op if the daemon is already loaded. Refuses when the operator has
    disabled the service: that decision belongs to them, not to a restore.
    """
    plist_path = plist_path or plist_path_for(label)
    if is_loaded(label):
        _LOG.info("daemon: %s already loaded", label)
        return

    if is_disabled(label):
        raise DaemonControlError(
            f"{label} is disabled by the operator (launchctl disable); "
            f"not starting it. Run `launchctl enable gui/{os.getuid()}/{label}` "
            f"first if you want it back."
        )

    if not plist_path.exists():
        raise DaemonControlError(
            f"Cannot start {label}: plist {plist_path} does not exist."
        )

    _LOG.info("daemon: loading %s", label)
    try:
        result = subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise DaemonControlError(f"launchctl load failed: {exc}") from exc
    if result.returncode != 0:
        raise DaemonControlError(
            f"launchctl load {plist_path} exit {result.returncode}: "
            f"{result.stderr.strip()}"
        )

    deadline = time.monotonic() + wait_timeout_seconds
    while time.monotonic() < deadline:
        if is_loaded(label):
            _LOG.info("daemon: %s confirmed up", label)
            return
        time.sleep(0.5)
    raise DaemonControlError(
        f"{label} did not appear in launchctl list within {wait_timeout_seconds}s."
    )
