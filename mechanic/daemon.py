"""Gateway daemon control for the supervised products.

Both OpenClaw (`ai.openclaw.gateway`) and Hermes (`ai.hermes.gateway`) run
their gateway as a user LaunchAgent that writes to the product's data
directory continuously. Restoring a snapshot is only safe with the daemon
stopped (otherwise the daemon recreates files in the data dir during the
untar, leaving a corrupted state).

This module wraps `launchctl unload/load` with a wait-until-down poll so the
restore flow knows when it is safe to mutate the data dir, reads
`launchctl print` for the one fact `launchctl list` cannot give (whether a
PID is actually alive), self-heals a gateway that died (v0.1.4), and reads
`launchctl print-disabled` so Mechanic can tell when the OPERATOR has
switched a gateway off on purpose.

Two scars live here. v0.1.4 (2026-09): `openclaw --version` answers happily
with a dead gateway, so four outages were reported "healthy" for their
whole duration, one for 43 hours; `check_gateway` is the liveness check
and the restart. v0.2.0 (2026-10-04): that very self-heal brought back a
gateway the operator had just booted out, four hours later, on the next
heartbeat. Mechanic had no way to know the operator meant it. Now it does:
a service the operator has `launchctl disable`d is never healed, never
started, and its product's CLI is never run.

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


def gui_domain() -> str:
    """launchd GUI per-user domain for the current uid, e.g. 'gui/502'."""
    return f"gui/{os.getuid()}"


def user_domains() -> tuple[str, str]:
    """The two per-user launchd domains an agent can live in.

    `gui/<uid>` is where a login session's agents go. `user/<uid>` is where
    an agent lands when it is loaded from a session with no GUI, such as an
    SSH login, which is how a headless Mac mini gets managed. A service in
    one is invisible to `launchctl print` in the other (found 2026-10-04:
    the Hermes gateway lived in user/502 while OpenClaw's lived in gui/502).
    """
    uid = os.getuid()
    return (f"gui/{uid}", f"user/{uid}")


def service_target(label: str, domain: Optional[str] = None) -> str:
    """Fully-qualified launchd service target, e.g. 'gui/502/ai.openclaw.gateway'."""
    return f"{domain or gui_domain()}/{label}"


def default_domain() -> str:
    """Where a plist loaded from this session would land.

    `launchctl managername` answers "Aqua" inside a GUI login session and
    "Background" (or similar) for SSH and other headless sessions.
    """
    gui, user = user_domains()
    try:
        result = subprocess.run(
            ["launchctl", "managername"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return gui
    return gui if "Aqua" in (result.stdout or "") else user


def find_service(label: str) -> tuple[Optional[str], Optional[int]]:
    """(domain, pid) for the first per-user domain that knows `label`.

    pid is None when the service is registered but has no live process.
    (None, None) when no domain knows it, or launchctl is unavailable.
    """
    for domain in user_domains():
        try:
            result = subprocess.run(
                ["launchctl", "print", service_target(label, domain)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None, None
        if result.returncode != 0:
            continue
        match = re.search(r"^\s*pid\s*=\s*(\d+)", result.stdout, re.MULTILINE)
        return domain, (int(match.group(1)) if match else None)
    return None, None


def running_pid(label: str) -> Optional[int]:
    """The gateway's pid if launchd reports it RUNNING, else None.

    This is the liveness check `is_loaded()` is not. `launchctl list` (and
    `launchctl print`) succeed for a service that is merely registered;
    only a `pid = N` line means a process is actually alive. The outages
    Mechanic failed to catch in 2026-07 and 2026-09 all left the service
    enabled-but-not-running, which `is_loaded()` and a `--version` probe
    both report as fine. Both per-user domains are consulted.
    """
    _, pid = find_service(label)
    return pid


def is_running(label: str) -> bool:
    """True when launchd reports a live pid for `label`."""
    return running_pid(label) is not None


def ensure_running(
    *,
    label: str,
    plist_path: Optional[Path] = None,
    wait_timeout_seconds: int = 60,
) -> tuple[bool, Optional[int]]:
    """Make sure the gateway is running; restart it if it is not.

    Returns `(restarted, pid)`. `restarted` is False when it was already up.

    Uses the per-domain API (`bootstrap` + `kickstart -k`) rather than the
    legacy `load`/`unload` used by stop()/start(). The failure mode we
    actually hit is "enabled but never bootstrapped", where `launchctl
    load` is unreliable and `kickstart -k` is the command verified to
    recover the host every time.

    Refuses, with DaemonControlError, when the operator has disabled the
    service: a deliberate shutdown is not an outage.
    """
    plist_path = plist_path or plist_path_for(label)
    domain, pid = find_service(label)
    if pid is not None:
        return False, pid

    if is_disabled(label):
        raise DaemonControlError(
            f"{label} is disabled by the operator (launchctl disable); not restarting it."
        )
    if not plist_path.exists():
        raise DaemonControlError(
            f"Cannot start {label}: plist {plist_path} does not exist."
        )

    # Kick it where it lives; a service nobody knows gets bootstrapped into
    # the domain this session would have put it in (Hermes's own rule).
    domain = domain or default_domain()
    target = service_target(label, domain)
    _LOG.warning("daemon: %s is not running; restarting via %s", label, target)

    # Best effort: no-ops (non-zero) when already bootstrapped, which is fine.
    try:
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _LOG.info("daemon: bootstrap of %s failed (continuing): %s", target, exc)

    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise DaemonControlError(f"launchctl kickstart {target} failed: {exc}") from exc
    if result.returncode != 0:
        raise DaemonControlError(
            f"launchctl kickstart {target} exit {result.returncode}: "
            f"{result.stderr.strip()}"
        )

    deadline = time.monotonic() + wait_timeout_seconds
    while time.monotonic() < deadline:
        pid = running_pid(label)
        if pid is not None:
            _LOG.info("daemon: %s confirmed running (pid %s)", label, pid)
            return True, pid
        time.sleep(1.0)

    raise DaemonControlError(
        f"{label} did not report a running pid within {wait_timeout_seconds}s "
        f"of kickstart."
    )


class GatewayCheck:
    """Outcome of a gateway liveness check and any repair that followed."""

    __slots__ = ("label", "pid", "installed", "was_down", "healed", "error")

    def __init__(
        self,
        *,
        label: str,
        pid: Optional[int],
        installed: bool = True,
        was_down: bool = False,
        healed: bool = False,
        error: Optional[str] = None,
    ) -> None:
        self.label = label
        self.pid = pid
        self.installed = installed
        self.was_down = was_down
        self.healed = healed
        self.error = error

    @property
    def ok(self) -> bool:
        """True when the gateway is running now, or there is none to run."""
        return self.pid is not None or not self.installed

    def summary(self) -> str:
        if not self.installed:
            return "No gateway service installed."
        if self.was_down and self.healed:
            return f"Gateway was DOWN; Mechanic restarted it (now pid {self.pid})."
        if self.was_down:
            return f"Gateway is DOWN and was not restarted: {self.error}"
        return f"Gateway running (pid {self.pid})."


def check_gateway(label: str, *, autoheal: bool = True) -> GatewayCheck:
    """Confirm the gateway process is alive, restarting it if allowed.

    This is the check a `--version` probe cannot make. Never raises: a
    failed or refused repair comes back as `ok == False` with `error` set,
    so an unattended caller can report it instead of crashing. No plist
    means no gateway is installed (a CLI-only install) and that is fine.
    """
    plist = plist_path_for(label)
    pid = running_pid(label)
    if pid is not None:
        return GatewayCheck(label=label, pid=pid)
    if not plist.exists():
        return GatewayCheck(label=label, pid=None, installed=False)

    _LOG.warning("gateway %s is not running", label)
    if is_disabled(label):
        return GatewayCheck(
            label=label, pid=None, was_down=True,
            error="disabled by the operator (launchctl disable); left alone",
        )
    if not autoheal:
        return GatewayCheck(label=label, pid=None, was_down=True, error="autoheal is off")

    try:
        _, new_pid = ensure_running(label=label, plist_path=plist)
    except DaemonControlError as exc:
        _LOG.error("gateway restart failed: %s", exc)
        return GatewayCheck(label=label, pid=None, was_down=True, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - unattended callers must never die here
        _LOG.error("gateway restart raised: %s", exc)
        return GatewayCheck(label=label, pid=None, was_down=True, error=str(exc))

    _LOG.warning("gateway %s restarted by Mechanic (pid %s)", label, new_pid)
    return GatewayCheck(label=label, pid=new_pid, was_down=True, healed=True)


def is_disabled(label: str) -> Optional[bool]:
    """Whether the operator has `launchctl disable`d this user service.

    Reads `launchctl print-disabled` for both per-user domains (gui/<uid>
    and user/<uid>); the output lists every service with an explicit
    enabled/disabled override as `"label" => disabled`. Returns True when
    the label is disabled in either domain, False when it is enabled or
    not listed in both, and None when launchctl is unavailable or every
    query failed (a non-macOS test box, or a launchd that refused).
    Callers treat None as "unknown, assume not disabled" and say so.
    """
    answered = False
    for domain in user_domains():
        try:
            result = subprocess.run(
                ["launchctl", "print-disabled", domain],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            continue
        answered = True
        if parse_disabled(result.stdout).get(label, False):
            return True
    return False if answered else None


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
            f"not starting it. Run `launchctl enable <domain>/{label}` "
            f"(gui/{os.getuid()} or user/{os.getuid()}, whichever "
            f"`launchctl print-disabled` lists it under) "
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
