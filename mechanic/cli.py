"""Command-line entry point for tekRESCUE Mechanic.

The CLI is the human-facing surface of Mechanic. Subcommands wire up logging,
load configuration, and dispatch to the right module. The launchd-scheduled
loops (supervisor and updater) have their own entry points so they can run
without going through the CLI; everything else hangs off the subparsers here.

Every command that acts on one product takes `--target openclaw|hermes`.
The default is openclaw when it is the only target configured, so a v0.1
install keeps its muscle memory; with several targets, commands that
mutate (restore, resume, capture-first-good) require `--target`.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

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
            "tekRESCUE Mechanic: a macOS supervisor that keeps OpenClaw and "
            "Hermes Agent updated a week behind the bleeding edge, with "
            "backups, verification, and a circuit breaker."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_target(p: argparse.ArgumentParser, *, help_text: str) -> None:
        p.add_argument("--target", default=None, help=help_text)

    sub.add_parser("status", help="Show current configuration, launchd state, and recent log lines.")

    plan_p = sub.add_parser("plan", help="Show what tonight's run would do for each target, without changing anything.")
    add_target(plan_p, help_text="Only this target (openclaw or hermes).")

    run_p = sub.add_parser("run-now", help="Run the nightly update routine immediately (foreground).")
    add_target(run_p, help_text="Only this target (openclaw or hermes). Default: every target in TARGETS.")

    resume_p = sub.add_parser("resume", help="Clear paused state and reset the failure counter.")
    add_target(resume_p, help_text="Which target to resume (openclaw or hermes).")

    restore = sub.add_parser("restore", help="Restore a target's data (and, for Hermes, code) from a snapshot.")
    restore.add_argument(
        "snapshot",
        help="Snapshot to restore: 'first-known-good', 'last-known-good', or a nightly id like 2026-05-23T02-00-00Z.",
    )
    add_target(restore, help_text="Which target to restore (openclaw or hermes).")

    cfg_p = sub.add_parser(
        "capture-first-good",
        help="Snapshot a target as first-known-good (only if currently healthy).",
    )
    add_target(cfg_p, help_text="Which target to snapshot (openclaw or hermes).")

    sub.add_parser(
        "capture-prompts",
        help="Run openclaw doctor interactively and record prompts for nightly replay.",
    )

    logs = sub.add_parser("logs", help="Print the tail of the mechanic log.")
    logs.add_argument("-n", "--lines", type=int, default=50, help="Number of lines to print (default 50).")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow the log (Ctrl-C to stop).")

    sub.add_parser("test-notifier", help="Send a test notification through the configured notifier.")

    install_p = sub.add_parser("install", help="Install LaunchAgents and create the config directory.")
    install_p.add_argument("--i-have-a-backup", action="store_true",
                            help="Skip the interactive backup confirmation. Use only if you have already backed up your agent's data.")

    uninstall_p = sub.add_parser("uninstall", help="Remove LaunchAgents and optionally purge data.")
    uninstall_p.add_argument("--purge", action="store_true", help="Also delete logs and snapshots.")

    args = parser.parse_args(argv)

    if args.command == "status":
        return _cmd_status()
    if args.command == "plan":
        return _cmd_plan(args.target)
    if args.command == "run-now":
        return _cmd_run_now(args.target)
    if args.command == "resume":
        return _cmd_resume(args.target)
    if args.command == "restore":
        return _cmd_restore(args.snapshot, args.target)
    if args.command == "capture-first-good":
        return _cmd_capture_first_good(args.target)
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


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


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
    from . import daemon
    from .rollback import store_for

    print("tekRESCUE Mechanic")
    print(f"  version: {__version__}")
    print()

    print("Configuration:")
    print(f"  file:                {CONFIG_FILE} ({_path_state(CONFIG_FILE, kind='file')})")
    print(f"  targets:             {', '.join(config.targets)}")
    print(f"  update time:         {config.update_time} local")
    print(f"  min update age:      {config.min_update_age_days} days (default for every target)")
    print(f"  supervisor interval: {config.supervisor_interval_minutes} min")
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

    if config.openclaw is not None:
        oc = config.openclaw
        print("OpenClaw:")
        print(f"  openclaw bin:        {oc.bin_path} ({_path_state(oc.bin_path, kind='file')})")
        print(f"  openclaw config:     {oc.config_path} ({_path_state(oc.config_path, kind='dir')})")
        print(f"  min update age:      {_openclaw_age_state(config)}")
        print(f"  prompt mode:         {config.prompt_mode}")
        if oc.skip_versions:
            print(f"  skip versions:       {', '.join(oc.skip_versions)}")
        print(f"  gateway:             {_gateway_state(daemon.OPENCLAW_DAEMON_LABEL)}")
        print(f"  state:               {_state_line('openclaw')}")
        print()

    if config.hermes is not None:
        hs = config.hermes
        from .hermes_release import checkout_present

        checkout = checkout_present(hs)
        print("Hermes:")
        print(f"  hermes bin:          {hs.bin_path} ({_path_state(hs.bin_path, kind='file')})")
        print(f"  hermes home:         {hs.home} ({_path_state(hs.home, kind='dir')})")
        print(f"  source checkout:     {hs.source_dir} ({'ok' if checkout is None else checkout})")
        print(f"  update mode:         {hs.update_mode}")
        if hs.update_channel:
            print(f"  update channel:      {hs.update_channel}")
        print(f"  min update age:      {hs.min_update_age_days} days")
        print(f"  doctor --fix:        {'yes' if hs.doctor_fix else 'no'}")
        if hs.skip_tags:
            print(f"  skip tags:           {', '.join(hs.skip_tags)}")
        print(f"  gateway:             {_gateway_state(daemon.HERMES_DAEMON_LABEL)}")
        print(f"  state:               {_state_line('hermes')}")
        print()

    print("Launchd:")
    print(f"  supervisor agent:    {_launchd_state(SUPERVISOR_LABEL, SUPERVISOR_PLIST)}")
    print(f"  updater agent:       {_launchd_state(UPDATER_LABEL, UPDATER_PLIST)}")
    print()

    print("Snapshots:")
    for name in config.targets:
        store = store_for(config, name)
        latest = _latest_snapshot(store.root)
        label = f"{name}:".ljust(10)
        print(f"  {label} {latest if latest is not None else 'none yet'}")
    print()

    print(f"Recent log ({LOG_FILE}):")
    _print_tail(LOG_FILE, lines=10)


def _openclaw_age_state(config: Config) -> str:
    """Describe OpenClaw's waiting period and whether npm can serve it."""
    from .release_age import find_npm

    days = config.openclaw.min_update_age_days if config.openclaw else config.min_update_age_days
    if days <= 0:
        return "0 days (waiting period off)"
    npm = find_npm(config)
    if npm is None:
        return (
            f"{days} days (npm NOT found next to openclaw or on PATH; "
            f"updates wait until it is)"
        )
    return f"{days} days (npm: {npm})"


def _gateway_state(label: str) -> str:
    from . import daemon

    disabled = daemon.is_disabled(label)
    loaded = daemon.is_loaded(label)
    if disabled:
        return f"{label} DISABLED by operator (Mechanic leaves this product alone)"
    if loaded:
        return f"{label} loaded"
    if disabled is None:
        return f"{label} not loaded (launchctl unavailable)"
    return f"{label} not loaded"


def _state_line(target: str) -> str:
    from .state import load_state

    state = load_state(target=target)
    if state.paused:
        return f"PAUSED since {state.paused_at} ({state.pause_reason})"
    parts = [f"{state.consecutive_failures} consecutive failures"]
    if state.last_success:
        parts.append(f"last success {state.last_success}")
    return ", ".join(parts)


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
    candidates = [
        p for p in snapshot_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "hermes"
    ]
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


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _cmd_plan(only: Optional[str]) -> int:
    """Dry run of the nightly's decision for each target. Nothing changes.

    The one write is Hermes's tag ledger (first-sight records), which is
    exactly what you want before the first supervised nightly: run this,
    read what Mechanic would pin to, then let the 02:00 fire do it.
    """
    from .targets import build_targets

    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=False)
    try:
        targets = build_targets(config, only=only)
    except ValueError as exc:
        print(f"{exc}")
        return 1

    worst = 0
    for target in targets:
        print(f"== {target.display_name} ==")
        if target.disabled_by_operator():
            print(f"  gateway {target.service_label} is DISABLED by the operator.")
            print(f"  Tonight: nothing. Mechanic does not run {target.display_name}'s CLI while it is disabled.")
            print()
            continue
        health = target.probe()
        print(f"  health:   {health.short_summary()}")
        if not health.healthy:
            print(f"  Tonight: skip the update, run doctor, verify. Reason: {health.reason}")
            print()
            worst = 1
            continue
        assessment = target.assess(health)
        print(f"  {assessment.reason}")
        for note in assessment.notes:
            print(f"  note: {note}")
        if assessment.install:
            plan = assessment.plan
            what = getattr(plan, "target_tag", None) or getattr(plan, "target_version", None)
            print(f"  Tonight: snapshot, install {what}, doctor, verify.")
        elif assessment.error:
            print("  Tonight: snapshot, NO install (could not establish the facts), doctor, verify.")
        else:
            print("  Tonight: snapshot, no install (waiting), doctor, verify.")
        print()
    return worst


# ---------------------------------------------------------------------------
# run-now / resume / restore / capture
# ---------------------------------------------------------------------------


def _cmd_run_now(only: Optional[str]) -> int:
    from .updater import run_updater
    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=True)
    try:
        return run_updater(config, only=only)
    except ValueError as exc:
        print(f"{exc}")
        return 1


def _pick_target(config: Config, requested: Optional[str], *, verb: str) -> Optional[str]:
    """Resolve --target: the only configured target when there is one."""
    if requested:
        if requested not in config.targets:
            print(f"{requested!r} is not in TARGETS ({', '.join(config.targets)}).")
            return None
        return requested
    if len(config.targets) == 1:
        return config.targets[0]
    print(f"Several targets are configured ({', '.join(config.targets)}).")
    print(f"Say which one to {verb}: --target openclaw or --target hermes.")
    return None


def _cmd_resume(requested: Optional[str]) -> int:
    from .state import load_state, resume, save_state
    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=False)
    target = _pick_target(config, requested, verb="resume")
    if target is None:
        return 1
    state = load_state(target=target)
    if not state.paused:
        print(f"{target}: not paused. Nothing to do.")
        return 0
    print(f"Resuming {target}. Was paused since {state.paused_at} ({state.pause_reason}).")
    save_state(resume(state), target=target)
    print(f"{target} is no longer paused. The next nightly run will proceed.")
    return 0


def _cmd_restore(snapshot_name: str, requested: Optional[str]) -> int:
    from . import daemon
    from .rollback import RollbackError, find_snapshot, restore_snapshot, store_for

    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=True)
    target = _pick_target(config, requested, verb="restore")
    if target is None:
        return 1
    store = store_for(config, target)

    try:
        snapshot = find_snapshot(store, snapshot_name)
    except RollbackError as exc:
        print(f"Snapshot {snapshot_name!r} is unreadable: {exc}")
        return 1
    if snapshot is None:
        print(f"No {target} snapshot named {snapshot_name!r}.")
        print(f"  Looked in {store.root}")
        return 1

    label = daemon.label_for(target)
    daemon_was_loaded = daemon.is_loaded(label)
    code_sha = snapshot.extra.get("code_sha") if target == "hermes" else None

    print(f"Restoring {target} data from {snapshot.path}")
    print(f"  source captured at: {snapshot.captured_at}")
    print(f"  version:            {snapshot.version or 'unknown'}")
    print(f"  destination:        {store.source}")
    print(f"  archive size:       {snapshot.archive_path.stat().st_size // (1024*1024)} MB")
    if code_sha:
        print(f"  code checkout:      will be put back on {code_sha[:12]} "
              f"({snapshot.extra.get('code_label') or 'untagged'}) and rebuilt with hermes pm install")
    print()
    if daemon_was_loaded:
        print(f"The {label} daemon is running and will be stopped")
        print("during the restore, then restarted afterwards.")
    else:
        print(f"The {label} daemon is not currently running.")
        print("Restore will proceed without daemon control.")
    print()
    answer = input("Proceed? This will overwrite the live data. [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted.")
        return 1

    if daemon_was_loaded:
        try:
            print(f"Stopping {label}...")
            daemon.stop(label=label)
        except daemon.DaemonControlError as exc:
            print(f"Could not stop daemon: {exc}")
            print("Restore aborted. Nothing was modified.")
            return 1

    try:
        print("Extracting archive over the data directory...")
        restore_snapshot(store, snapshot)
    except RollbackError as exc:
        print(f"Restore failed: {exc}")
        _restart_after_restore(daemon_was_loaded, label)
        return 1

    if code_sha:
        rc = _restore_hermes_code(config, code_sha)
        if rc != 0:
            _restart_after_restore(daemon_was_loaded, label)
            return rc

    print("Restore complete.")
    return _restart_after_restore(daemon_was_loaded, label)


def _restore_hermes_code(config: Config, code_sha: str) -> int:
    from .hermes_release import _git, _hermes, PM_INSTALL_TIMEOUT_SECONDS

    assert config.hermes is not None
    spec = config.hermes
    print(f"Putting the Hermes checkout back on {code_sha[:12]}...")
    result = _git(spec, "checkout", "--quiet", "--detach", code_sha)
    if not result.ok:
        print(f"  git checkout failed: {result.tail()}")
        return 1
    print("Rebuilding the dependency environment (hermes pm install)...")
    result = _hermes(spec, "pm", "install", timeout=PM_INSTALL_TIMEOUT_SECONDS)
    if not result.ok:
        print(f"  hermes pm install failed: {result.tail()}")
        return 1
    print("  code restored.")
    return 0


def _restart_after_restore(was_loaded: bool, label: str) -> int:
    from . import daemon

    if not was_loaded:
        return 0
    try:
        print(f"Restarting {label}...")
        daemon.start(label=label)
    except daemon.DaemonControlError as exc:
        print(f"Daemon restart failed: {exc}")
        print(f"Run `launchctl load {LAUNCHAGENTS_DIR}/{label}.plist` manually.")
        return 1
    print(f"{label} is back up.")
    return 0


def _cmd_capture_first_good(requested: Optional[str]) -> int:
    from .rollback import KIND_FIRST_KNOWN_GOOD, RollbackError, capture_snapshot
    from .state import load_state, mark_first_known_good_captured, save_state
    from .targets import build_targets

    config = _load_config_or_die()
    if config is None:
        return 1
    _configure_logging(config, also_stderr=True)
    name = _pick_target(config, requested, verb="snapshot")
    if name is None:
        return 1
    target = build_targets(config, only=name)[0]

    print(f"Verifying {target.display_name} is healthy before snapshotting...")
    result = target.probe()
    if not result.healthy:
        print(f"{target.display_name} is NOT healthy ({result.short_summary()}). Refusing to capture first-known-good.")
        print(f"Fix {target.display_name}, then run `mechanic capture-first-good --target {name}` again.")
        return 1
    print(f"{target.display_name} is healthy: {result.short_summary()}")
    pre_note = target.pre_snapshot()
    if pre_note:
        print(f"  {pre_note}")
    try:
        snapshot = capture_snapshot(
            target.store(),
            kind=KIND_FIRST_KNOWN_GOOD,
            version=result.version,
            verified_healthy=True,
            extra=target.snapshot_extra(),
        )
    except RollbackError as exc:
        print(f"Snapshot failed: {exc}")
        return 1
    save_state(mark_first_known_good_captured(load_state(target=name)), target=name)
    print(f"Captured first-known-good at {snapshot.path}")
    return 0


def _cmd_capture_prompts() -> int:
    from .doctor_runner import capture_prompts
    config = _load_config_or_die()
    if config is None:
        return 1
    if config.openclaw is None:
        print("capture-prompts is an OpenClaw command and openclaw is not in TARGETS.")
        return 1
    _configure_logging(config, also_stderr=False)
    capture_prompts(config)
    return 0


# ---------------------------------------------------------------------------
# logs / notifier / install / uninstall
# ---------------------------------------------------------------------------


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
