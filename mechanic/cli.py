"""Command-line entry point for tekRESCUE Mechanic.

The CLI is the human-facing surface of Mechanic. Subcommands wire up logging,
load configuration, and dispatch to the right module. The launchd-scheduled
loops (supervisor and updater) have their own entry points so they can run
without going through the CLI; everything else hangs off the subparsers here.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import (
    CONFIG_FILE,
    LOG_FILE,
    SNAPSHOT_DIR,
    Config,
    ConfigError,
    load_config,
)
from .logging_setup import configure_logging


SUPERVISOR_LABEL = "com.tekrescue.mechanic.supervisor"
UPDATER_LABEL = "com.tekrescue.mechanic.updater"

LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SUPERVISOR_PLIST = LAUNCHAGENTS_DIR / f"{SUPERVISOR_LABEL}.plist"
UPDATER_PLIST = LAUNCHAGENTS_DIR / f"{UPDATER_LABEL}.plist"


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `mechanic` console script."""
    parser = argparse.ArgumentParser(
        prog="mechanic",
        description=(
            "tekRESCUE Mechanic for OpenClaw: a macOS supervisor that "
            "auto-heals OpenClaw after broken updates."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show current configuration, launchd state, and recent log lines.")
    sub.add_parser("run-now", help="Run the nightly update routine immediately (foreground).")
    sub.add_parser("resume", help="Clear paused state and reset the failure counter.")

    restore = sub.add_parser("restore", help="Restore OpenClaw config from a snapshot.")
    restore.add_argument(
        "target",
        help="Snapshot to restore: 'first-known-good', 'last-known-good', or a nightly id like 2026-05-23T02-00-00Z.",
    )

    sub.add_parser(
        "capture-first-good",
        help="Snapshot OpenClaw as first-known-good (only if currently healthy).",
    )
    sub.add_parser(
        "capture-prompts",
        help="Run doctor interactively and record prompts for nightly replay.",
    )

    logs = sub.add_parser("logs", help="Print the tail of the mechanic log.")
    logs.add_argument("-n", "--lines", type=int, default=50, help="Number of lines to print (default 50).")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow the log (Ctrl-C to stop).")

    sub.add_parser("test-notifier", help="Send a test notification through the configured notifier.")

    install_p = sub.add_parser("install", help="Install LaunchAgents and create the config directory.")
    install_p.add_argument("--i-have-a-backup", action="store_true",
                            help="Skip the interactive backup confirmation. Use only if you have already backed up OpenClaw's config.")

    uninstall_p = sub.add_parser("uninstall", help="Remove LaunchAgents and optionally purge data.")
    uninstall_p.add_argument("--purge", action="store_true", help="Also delete logs and snapshots.")

    args = parser.parse_args(argv)

    if args.command == "status":
        return _cmd_status()
    if args.command == "run-now":
        return _cmd_run_now()
    if args.command == "resume":
        return _cmd_resume()
    if args.command == "restore":
        return _cmd_restore(args.target)
    if args.command == "capture-first-good":
        return _cmd_capture_first_good()
    if args.command == "capture-prompts":
        return _cmd_capture_prompts()
    if args.command == "logs":
        return _cmd_logs(args.lines, args.follow)
    if args.command == "test-notifier":
        return _cmd_test_notifier()
    if args.command == "install":
        return _cmd_install(skip_warning=args.i_have_a_backup)
    if args.command == "uninstall":
        return _cmd_uninstall(purge=args.purge)

    parser.error(f"Unknown command: {args.command}")
    return 2


def _cmd_status() -> int:
    """Print a snapshot of the current Mechanic installation."""
    try:
        config = load_config()
    except ConfigError as exc:
        print("Configuration error.")
        print(f"  {exc}")
        print(f"  Edit {CONFIG_FILE} and run mechanic status again.")
        return 1

    configure_logging(
        LOG_FILE,
        level=config.log_level,
        secrets=config.secret_values(),
        also_stderr=False,
    )
    logging.getLogger("mechanic.cli").debug("status command invoked")

    _print_status(config)
    return 0


def _print_status(config: Config) -> None:
    print("tekRESCUE Mechanic for OpenClaw")
    print(f"  version: {__version__}")
    print()

    print("Configuration:")
    print(f"  file:                {CONFIG_FILE} ({_path_state(CONFIG_FILE, kind='file')})")
    print(
        f"  openclaw bin:        {config.openclaw_bin_path} "
        f"({_path_state(config.openclaw_bin_path, kind='file')})"
    )
    print(
        f"  openclaw config:     {config.openclaw_config_path} "
        f"({_path_state(config.openclaw_config_path, kind='dir')})"
    )
    print(f"  update time:         {config.update_time} local")
    print(f"  min update age:      {_min_update_age_state(config)}")
    print(f"  supervisor interval: {config.supervisor_interval_minutes} min")
    print(f"  prompt mode:         {config.prompt_mode}")
    print(f"  snapshot retention:  {config.snapshot_retention_days} days")
    print(f"  min free disk:       {config.min_free_disk_mb_for_snapshot} MB")
    print(f"  max failures:        {config.max_consecutive_failures}")
    print(
        f"  pause on rollback:   "
        f"{'yes' if config.pause_on_rollback_failure else 'no'}"
    )
    print(f"  log level:           {config.log_level}")
    print(f"  notifier:            {config.notifier.kind}")
    print()

    print("Launchd:")
    print(f"  supervisor agent:    {_launchd_state(SUPERVISOR_LABEL, SUPERVISOR_PLIST)}")
    print(f"  updater agent:       {_launchd_state(UPDATER_LABEL, UPDATER_PLIST)}")
    print()

    print("Snapshots:")
    latest = _latest_snapshot(SNAPSHOT_DIR)
    if latest is None:
        print("  none yet")
    else:
        print(f"  latest: {latest}")
    print()

    print(f"Recent log ({LOG_FILE}):")
    _print_tail(LOG_FILE, lines=10)


def _min_update_age_state(config: Config) -> str:
    """Describe the release waiting period and whether npm can serve it."""
    from .release_age import find_npm

    days = config.min_update_age_days
    if days <= 0:
        return "0 days (waiting period off)"
    npm = find_npm(config)
    if npm is None:
        return (
            f"{days} days (npm NOT found next to openclaw or on PATH; "
            f"updates wait until it is)"
        )
    return f"{days} days (npm: {npm})"


def _path_state(path: Path, *, kind: str) -> str:
    if not path.exists():
        return "missing"
    if kind == "file" and not path.is_file():
        return "exists but is not a file"
    if kind == "dir" and not path.is_dir():
        return "exists but is not a directory"
    return "ok"


def _launchd_state(label: str, plist: Path) -> str:
    if not plist.exists():
        return "not installed"
    launchctl = shutil.which("launchctl")
    if not launchctl:
        return "plist installed, launchctl not on PATH"
    try:
        result = subprocess.run(
            [launchctl, "list", label],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return "plist installed, launchctl probe failed"
    if result.returncode == 0:
        return "loaded"
    return "plist installed, not loaded"


def _latest_snapshot(snapshot_dir: Path) -> Path | None:
    if not snapshot_dir.exists():
        return None
    candidates = [p for p in snapshot_dir.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _print_tail(log_file: Path, *, lines: int) -> None:
    if not log_file.exists():
        print("  no log yet")
        return
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as handle:
            buffer = handle.readlines()
    except OSError as exc:
        print(f"  could not read log: {exc}")
        return
    if not buffer:
        print("  log is empty")
        return
    for line in buffer[-lines:]:
        print(f"  {line.rstrip()}")


def _cmd_run_now() -> int:
    from .updater import run_updater
    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=True)
    return run_updater(config)


def _cmd_resume() -> int:
    from .state import load_state, resume, save_state
    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=False)
    state = load_state()
    if not state.paused:
        print("Not paused. Nothing to do.")
        return 0
    print(f"Resuming. Was paused since {state.paused_at} ({state.pause_reason}).")
    save_state(resume(state))
    print("Mechanic is no longer paused. The next nightly run will proceed.")
    return 0


def _cmd_restore(target: str) -> int:
    from .daemon import DaemonControlError, is_loaded, start, stop, OPENCLAW_DAEMON_LABEL
    from .rollback import (
        KIND_FIRST_KNOWN_GOOD,
        KIND_LAST_KNOWN_GOOD,
        NIGHTLY_DIR,
        RollbackError,
        get_sticky,
        restore_snapshot,
        _snapshot_from_path,
    )
    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=True)

    if target in (KIND_FIRST_KNOWN_GOOD, KIND_LAST_KNOWN_GOOD):
        snapshot = get_sticky(target)
        if snapshot is None:
            print(f"No {target} snapshot exists yet.")
            return 1
    else:
        candidate = NIGHTLY_DIR / target
        if not candidate.is_dir():
            print(f"No nightly snapshot named {target!r}.")
            print(f"  Looked in {NIGHTLY_DIR}")
            return 1
        try:
            snapshot = _snapshot_from_path(candidate)
        except RollbackError as exc:
            print(f"Snapshot at {candidate} is unreadable: {exc}")
            return 1

    daemon_was_loaded = is_loaded()

    print(f"Restoring OpenClaw config from {snapshot.path}")
    print(f"  source captured at: {snapshot.captured_at}")
    print(f"  openclaw version:   {snapshot.openclaw_version or 'unknown'}")
    print(f"  destination:        {config.openclaw_config_path}")
    print(f"  archive size:       {snapshot.archive_path.stat().st_size // (1024*1024)} MB")
    print()
    if daemon_was_loaded:
        print(f"The {OPENCLAW_DAEMON_LABEL} daemon is running and will be stopped")
        print("during the restore, then restarted afterwards.")
    else:
        print(f"The {OPENCLAW_DAEMON_LABEL} daemon is not currently running.")
        print("Restore will proceed without daemon control.")
    print()
    answer = input("Proceed? This will overwrite the live config. [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted.")
        return 1

    if daemon_was_loaded:
        try:
            print(f"Stopping {OPENCLAW_DAEMON_LABEL}...")
            stop()
        except DaemonControlError as exc:
            print(f"Could not stop daemon: {exc}")
            print("Restore aborted. OpenClaw config was not modified.")
            return 1

    try:
        print("Extracting archive over OpenClaw config directory...")
        restore_snapshot(config, snapshot)
    except RollbackError as exc:
        print(f"Restore failed: {exc}")
        if daemon_was_loaded:
            print(f"Restarting {OPENCLAW_DAEMON_LABEL} (best effort)...")
            try:
                start()
            except DaemonControlError as start_exc:
                print(f"  daemon restart also failed: {start_exc}")
                print(f"  run `launchctl load {Path.home()}/Library/LaunchAgents/{OPENCLAW_DAEMON_LABEL}.plist` manually")
        return 1

    print("Restore complete.")
    if daemon_was_loaded:
        try:
            print(f"Restarting {OPENCLAW_DAEMON_LABEL}...")
            start()
        except DaemonControlError as exc:
            print(f"Daemon restart failed: {exc}")
            print(f"Run `launchctl load {Path.home()}/Library/LaunchAgents/{OPENCLAW_DAEMON_LABEL}.plist` manually.")
            return 1
        print(f"{OPENCLAW_DAEMON_LABEL} is back up.")
    return 0


def _cmd_capture_first_good() -> int:
    from .rollback import (
        KIND_FIRST_KNOWN_GOOD,
        RollbackError,
        capture_snapshot,
    )
    from .state import load_state, mark_first_known_good_captured, save_state
    from .verifier import verify_openclaw

    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=True)

    print("Verifying OpenClaw is healthy before snapshotting...")
    result = verify_openclaw(config)
    if not result.healthy:
        print(f"OpenClaw is NOT healthy ({result.short_summary()}). Refusing to capture first-known-good.")
        print("Fix OpenClaw, then run `mechanic capture-first-good` again.")
        return 1
    print(f"OpenClaw is healthy: {result.short_summary()}")
    try:
        snapshot = capture_snapshot(
            config,
            kind=KIND_FIRST_KNOWN_GOOD,
            openclaw_version=result.version,
            verified_healthy=True,
        )
    except RollbackError as exc:
        print(f"Snapshot failed: {exc}")
        return 1
    save_state(mark_first_known_good_captured(load_state()))
    print(f"Captured first-known-good at {snapshot.path}")
    return 0


def _cmd_capture_prompts() -> int:
    from .doctor_runner import capture_prompts
    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=False)
    capture_prompts(config)
    return 0


def _cmd_logs(lines: int, follow: bool) -> int:
    if not LOG_FILE.exists():
        print(f"No log file at {LOG_FILE} yet.")
        return 0
    if not follow:
        _print_tail(LOG_FILE, lines=lines)
        return 0
    tail_bin = shutil.which("tail")
    if not tail_bin:
        print("`tail` not on PATH; cannot follow.")
        return 1
    try:
        return subprocess.call([tail_bin, "-n", str(lines), "-f", str(LOG_FILE)])
    except KeyboardInterrupt:
        return 0


def _cmd_test_notifier() -> int:
    from .notifier import send
    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=True)
    if config.notifier.kind == "none":
        print("NOTIFIER=none. Nothing to test.")
        print("Set NOTIFIER in your .env to telegram, slack, webhook, or email.")
        return 1
    print(f"Sending a test message via notifier kind={config.notifier.kind}...")
    result = send(
        config,
        "tekRESCUE Mechanic: test notification",
        "If you can read this, your Mechanic notifier is wired up correctly.",
    )
    if result.delivered:
        print("Delivered.")
        return 0
    print(f"Delivery failed: {result.error}")
    return 1


def _cmd_install(*, skip_warning: bool) -> int:
    script = _repo_root() / "install.sh"
    if not script.exists():
        print(f"install.sh not found at {script}.")
        print("Clone the full repository and run mechanic install from inside it.")
        return 1
    cmd = [str(script)]
    if skip_warning:
        cmd.append("--i-have-a-backup")
    return subprocess.call(cmd)


def _cmd_uninstall(*, purge: bool) -> int:
    script = _repo_root() / "uninstall.sh"
    if not script.exists():
        print(f"uninstall.sh not found at {script}.")
        print("Clone the full repository and run mechanic uninstall from inside it.")
        return 1
    cmd = [str(script)]
    if purge:
        cmd.append("--purge")
    return subprocess.call(cmd)


def _load_config_or_die() -> Config | None:
    try:
        return load_config()
    except ConfigError as exc:
        print("Configuration error.")
        print(f"  {exc}")
        print(f"  Edit {CONFIG_FILE} and try again.")
        return None


def _configure_logging(config: Config, *, also_stderr: bool) -> None:
    configure_logging(
        LOG_FILE,
        level=config.log_level,
        secrets=config.secret_values(),
        also_stderr=also_stderr,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


if __name__ == "__main__":
    sys.exit(main())
