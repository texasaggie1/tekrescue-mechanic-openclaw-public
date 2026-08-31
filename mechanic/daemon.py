"""OpenClaw gateway daemon control.

The `ai.openclaw.gateway` LaunchAgent runs continuously and writes to
OPENCLAW_CONFIG_PATH whenever Mechanic does. Restoring a snapshot is only
safe with the daemon stopped (otherwise the daemon recreates files in the
config dir during the untar, leaving a corrupted state).

This module wraps `launchctl unload/load` and provides a wait-until-down
poll so the restore flow knows when it is safe to mutate the config dir.
The daemon is identified by its label and the absolute path to its plist;
both are user-level (not root). v0.1 hard-codes the standard install path;
make it a config variable if/when OpenClaw ships in non-standard locations.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional


OPENCLAW_DAEMON_LABEL = "ai.openclaw.gateway"
OPENCLAW_DAEMON_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / f"{OPENCLAW_DAEMON_LABEL}.plist"
)

_LOG = logging.getLogger(__name__)


class DaemonControlError(Exception):
    """Raised when stopping or starting the OpenClaw daemon fails."""


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


def stop(
    *,
    label: str = OPENCLAW_DAEMON_LABEL,
    plist_path: Path = OPENCLAW_DAEMON_PLIST,
    wait_timeout_seconds: int = 30,
) -> None:
    """Unload the daemon and poll until it is no longer listed.

    No-op if the daemon was not loaded to begin with.
    """
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
    plist_path: Path = OPENCLAW_DAEMON_PLIST,
    wait_timeout_seconds: int = 30,
) -> None:
    """Load the daemon and poll until it is listed.

    No-op if the daemon is already loaded.
    """
    if is_loaded(label):
        _LOG.info("daemon: %s already loaded", label)
        return

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
