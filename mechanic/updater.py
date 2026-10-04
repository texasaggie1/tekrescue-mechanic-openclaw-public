"""Nightly update routine (v0.2.0).

Runs once per target in TARGETS order (see targets.py). For each target:

  0. Read the target's supervisor state. If paused, log the reason and
     move on. If the operator has `launchctl disable`d the target's
     gateway, skip the target entirely without running its CLI: OpenClaw's
     CLI restarts its own gateway when probed (SESSIONS.md 2026-10-04).
  1. Pre-flight disk check against the target's snapshot store.
  2. Pre-update verify. If the target is already broken, skip the update
     and run doctor anyway in case it can fix the existing breakage.
  3. Capture a nightly snapshot as a tar.gz archive (see rollback.py).
     Hermes first runs `hermes backup --quick` so a SQLite-safe copy of its
     state lands inside the archive. `--no-snapshot` (manual runs only)
     skips this and the last-known-good refresh: the two tars are most of
     a run's wall-clock, and a developer iterating on doctor or verify does
     not need a rollback point every time. The report says so in capitals.
  4. Assess. OpenClaw: `openclaw update status --json`, then the npm
     waiting period (release_age.py). Hermes: the release-tag waiting
     period against origin's tags (hermes_release.py). Anything the
     assessment cannot establish resolves to "leave the install alone"
     and is spelled out in the morning report.
  5. Apply, when the assessment says so. OpenClaw: `openclaw update --yes
     --tag <version>`. Hermes: pin the checkout and `hermes pm install`
     (or `hermes update --yes` in hermes-update mode), then a gateway
     restart if one was running and not disabled.
  6. Doctor: `openclaw doctor --fix --non-interactive` or
     `hermes doctor [--fix]`. Exit codes are informational.
  7. OpenClaw only: POST_UPDATE_HOOK after a real update, before verify.
  8. Post-update verify. **The authoritative health signal.** Hermes also
     checks the checkout landed on the expected commit, `hermes pm status`,
     and `hermes gateway status` after a restart.
  8b. Gateway liveness (v0.1.4). A `--version` probe answers happily with a
     dead gateway; that is how a 2026-09 morning report said SUCCESS while
     the gateway had been down for two minutes and stayed down for 43
     hours. Anything in this routine can stop a gateway, so confirm it has
     a live pid afterwards and, where autoheal is on, restart it. The
     Verify line carries the result. A gateway the operator has disabled
     is never restarted.
  9. On verify failure: log + notify, increment consecutive_failures,
     auto-pause at MAX_CONSECUTIVE_FAILURES. **NO auto-rollback.** The
     morning report tells the operator: `mechanic restore --target <t> <id>`.
 10. On verify success: reset consecutive_failures, stamp last_success,
     refresh last-known-good.
 11. Prune nightlies down to SNAPSHOT_RETENTION_DAYS entries.
 12. One morning report covering every target, to the log and notifier.

Entry points: `run_updater(config)` returns 0 healthy, 1 soft-fail (a
verify failed but Mechanic stayed up), 2 hard-fail (paused or pre-flight
aborted), the worst across targets. `main()` is the launchd-invoked CLI
wrapper.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from .config import Config, LOG_FILE, clean_subprocess_env, load_config
from .logging_setup import configure_logging
from .notifier import send as notify_send
from .reporter import RunReport, format_reports, short_subject
from .rollback import (
    InsufficientDiskError,
    KIND_LAST_KNOWN_GOOD,
    KIND_NIGHTLY,
    RollbackError,
    Snapshot,
    capture_snapshot,
    ensure_disk_headroom,
    prune_nightlies,
)
from .state import (
    SupervisorState,
    load_state,
    record_failure,
    record_run_start,
    record_success,
    save_state,
)
from .targets import Target, UpdateOutcome, build_targets
from .verifier import extract_version


_LOG = logging.getLogger(__name__)

UPDATE_TIMEOUT_SECONDS = 600


def run_updater(
    config: Config,
    *,
    test_fire: bool = False,
    only: Optional[str] = None,
    no_snapshot: bool = False,
) -> int:
    """Execute the nightly routine for every target. Returns the worst exit code.

    When `test_fire=True`, the install step is skipped for every target.
    Everything else runs as normal: snapshot, doctor, post-verify,
    last-known-good refresh, prune, morning report. Used by install.sh to
    surface any first-run macOS permission prompts in the foreground.
    `only` restricts the run to one target (`mechanic run-now --target`).
    `no_snapshot=True` skips the nightly snapshot and the last-known-good
    refresh (CLI-only; the launchd nightly never sets it).
    """
    targets = build_targets(config, only=only)
    reports: list[RunReport] = []
    states: dict[str, SupervisorState] = {}
    worst = 0
    for target in targets:
        report, state, code = _run_target(
            config, target, test_fire=test_fire, no_snapshot=no_snapshot
        )
        reports.append(report)
        states[target.name] = state
        worst = max(worst, code)
    _emit_reports(config, reports, states)
    return worst


def _run_target(
    config: Config, target: Target, *, test_fire: bool, no_snapshot: bool = False
) -> tuple[RunReport, SupervisorState, int]:
    name = target.name
    started_at = _now_iso()
    state = load_state(target=name)

    if state.paused:
        _LOG.warning(
            "updater[%s]: paused (%s since %s); skipping",
            name, state.pause_reason, state.paused_at,
        )
        report = RunReport(
            target=name, started_at=started_at, finished_at=_now_iso(),
            paused=True, pause_reason=state.pause_reason, paused_at=state.paused_at,
            notes=[f"Updater skipped {target.display_name} because its supervisor state is paused."],
        )
        return report, state, 2

    # 0b. Operator off switch: a disabled gateway means hands off, CLI and all.
    if target.disabled_by_operator():
        _LOG.warning(
            "updater[%s]: gateway %s is disabled by the operator; not running %s at all",
            name, target.service_label, target.display_name,
        )
        report = RunReport(
            target=name, started_at=started_at, finished_at=_now_iso(),
            overall="skipped",
            notes=[
                f"{target.display_name}'s gateway ({target.service_label}) is disabled "
                f"by the operator (launchctl disable). Mechanic did not run "
                f"{target.display_name}'s CLI. Enable it, or drop {name} from TARGETS, "
                f"to change that."
            ],
        )
        return report, state, 0

    state = record_run_start(state)
    save_state(state, target=name)

    # 1. Pre-flight disk check. May emergency-prune the oldest nightly
    # snapshots to make room for this run; see ensure_disk_headroom.
    store = target.store()
    try:
        emergency_pruned = ensure_disk_headroom(
            store, min_free_mb=config.min_free_disk_mb_for_snapshot
        )
    except InsufficientDiskError as exc:
        _LOG.error("updater[%s]: %s", name, exc)
        report = RunReport(
            target=name, started_at=started_at, finished_at=_now_iso(),
            overall="aborted", notes=[f"Pre-flight disk check failed: {exc}"],
        )
        return report, state, 2

    # 2. Pre-update verify.
    pre_verify = target.probe()
    version_before = pre_verify.version
    was_broken = not pre_verify.healthy
    if was_broken:
        _LOG.warning(
            "updater[%s]: %s is already broken pre-update (%s); "
            "skipping update step, running doctor anyway",
            name, target.display_name, pre_verify.short_summary(),
        )

    # 3. Snapshot.
    notes: list[str] = []
    snapshot: Optional[Snapshot] = None
    try:
        if no_snapshot:
            _LOG.warning(
                "updater[%s]: --no-snapshot: skipping nightly snapshot; "
                "NO rollback point for this run", name,
            )
        else:
            pre_note = target.pre_snapshot()
            if pre_note:
                notes.append(f"Pre-snapshot: {pre_note}")
            snapshot = capture_snapshot(
                store, kind=KIND_NIGHTLY, version=version_before,
                verified_healthy=pre_verify.healthy, extra=target.snapshot_extra(),
            )
    except RollbackError as exc:
        _LOG.error("updater[%s]: snapshot capture failed: %s", name, exc)
        report = RunReport(
            target=name, started_at=started_at, finished_at=_now_iso(),
            overall="aborted", notes=notes + [f"Snapshot capture failed: {exc}"],
        )
        return report, state, 2

    # 4 + 5. Assess and apply.
    outcome: Optional[UpdateOutcome] = None
    waiting_note: Optional[str] = None
    if test_fire:
        update_summary = f"skipped (--test-fire; {target.display_name} update not invoked)"
    elif was_broken:
        update_summary = f"skipped ({target.display_name} already broken pre-update)"
    else:
        assessment = target.assess(pre_verify)
        waiting_note = assessment.waiting_note
        notes.extend(assessment.notes)
        if not assessment.install:
            update_summary = assessment.skip_summary
            level = _LOG.warning if assessment.error else _LOG.info
            level("updater[%s]: %s", name, assessment.reason)
        else:
            _LOG.info("updater[%s]: %s", name, assessment.reason)
            outcome = target.apply(assessment)
            update_summary = outcome.summary
            notes.extend(f"Step: {step}" for step in outcome.notes)

    # 6. Doctor.
    doctor = target.doctor()

    # 7. Post-install hook (OpenClaw): re-apply operator patches after any
    # run that invoked the product's own updater, before post-verify.
    hook_summary: Optional[str] = None
    if outcome is not None and outcome.install_modified:
        hook_summary = target.post_install_hook(outcome)

    # 8. Post-update verify: the authoritative signal.
    post_verify = target.verify(outcome)
    verify_summary = post_verify.short_summary()
    version_after = post_verify.version

    # 8b. Gateway liveness, folded into the Verify line so it can never
    # again read "healthy" while the gateway is dead.
    gateway = target.check_gateway()
    if gateway.was_down:
        _LOG.error("updater[%s]: %s", name, gateway.summary())
    else:
        _LOG.info("updater[%s]: %s", name, gateway.summary())
    verify_summary = f"{verify_summary}; {gateway.summary()}"

    healthy = post_verify.healthy and doctor.aborted_prompt is None and gateway.ok
    doctor_warning = (not doctor.success) and doctor.aborted_prompt is None

    rollback_summary: Optional[str] = None
    if healthy:
        if no_snapshot:
            _LOG.warning("updater[%s]: --no-snapshot: not refreshing last-known-good", name)
            state = record_success(state)
        else:
            try:
                lkg = capture_snapshot(
                    store, kind=KIND_LAST_KNOWN_GOOD, version=version_after,
                    verified_healthy=True, extra=target.snapshot_extra(),
                )
                state = record_success(state, snapshot_id=lkg.snapshot_id)
            except RollbackError as exc:
                _LOG.warning("updater[%s]: could not refresh last-known-good: %s", name, exc)
                state = record_success(
                    state, snapshot_id=snapshot.snapshot_id if snapshot else None
                )
        overall = "success"
    else:
        # No auto-rollback: restoring means stopping the live daemon, which
        # is too risky unattended. The operator decides.
        target_flag = "" if name == "openclaw" else f"--target {name} "
        if snapshot is not None:
            restore_cmd = f"mechanic restore {target_flag}{snapshot.snapshot_id}"
            _LOG.warning(
                "updater[%s]: verify failed; NOT auto-rolling back. Run `%s` to revert.",
                name, restore_cmd,
            )
            rollback_summary = f"NOT auto-rolled back. To revert: {restore_cmd}"
        else:
            _LOG.warning(
                "updater[%s]: verify failed and --no-snapshot was set; no snapshot "
                "from this run to restore. `mechanic snapshots %s` lists earlier ones.",
                name, target_flag.strip(),
            )
            rollback_summary = (
                f"NOT auto-rolled back, and no snapshot was taken this run "
                f"(--no-snapshot). `mechanic snapshots {target_flag.strip()}` lists earlier ones."
            )
        state = record_failure(state, max_consecutive_failures=config.max_consecutive_failures)
        overall = "failed"

    # 11. Prune.
    pruned = prune_nightlies(store, keep=config.snapshot_retention_days)
    if pruned:
        _LOG.info("updater[%s]: pruned %d nightly snapshots", name, len(pruned))

    save_state(state, target=name)

    # 12. Report facts.
    if emergency_pruned:
        notes.insert(0, (
            f"Disk: emergency-pruned {len(emergency_pruned)} oldest nightly "
            f"snapshot(s) to make room for this run: {', '.join(emergency_pruned)}"
        ))
    if no_snapshot:
        notes.append(
            "SNAPSHOTS SKIPPED (--no-snapshot): no nightly snapshot and no "
            "last-known-good refresh were captured this run. There is NO rollback "
            "point from this run. Manual/developer runs only."
        )
    if test_fire:
        notes.append(
            "TEST FIRE: this run was triggered by install.sh to surface any "
            "first-run macOS permission prompts. The update step was "
            "deliberately skipped; everything else ran normally."
        )
    if doctor.aborted_prompt:
        notes.append(
            f"Doctor aborted on unrecognised prompt: {doctor.aborted_prompt!r}. "
            f"Run `mechanic capture-prompts` to teach Mechanic the answer."
        )
    if was_broken:
        notes.append(f"{target.display_name} was already unhealthy pre-update; update step was skipped.")
    if waiting_note:
        notes.append(waiting_note)
    if doctor_warning:
        code = f" ({doctor.exit_code})" if doctor.exit_code is not None else ""
        notes.append(
            f"Doctor exited non-zero{code} but verify still passed. This usually "
            f"means doctor reported warnings it could not auto-fix."
        )

    report = RunReport(
        target=name,
        started_at=started_at,
        finished_at=_now_iso(),
        paused=state.paused,
        pause_reason=state.pause_reason,
        paused_at=state.paused_at,
        snapshot_id=snapshot.snapshot_id if snapshot else None,
        version_before=version_before,
        version_after=version_after,
        update_summary=update_summary,
        install_modified=bool(outcome and outcome.install_modified),
        hook_summary=hook_summary,
        hook_label=target.post_install_label,
        doctor_summary=doctor.summary,
        verify_summary=verify_summary,
        rollback_summary=rollback_summary,
        overall=overall,
        notes=notes,
    )
    if state.paused:
        return report, state, 2
    if not healthy:
        return report, state, 1
    return report, state, 0


def run_openclaw_update(config: Config, *, target_version: Optional[str] = None) -> str:
    """Run `openclaw update --yes`, pinned with `--tag` when the plan says so.

    `--tag <version>` is OpenClaw's own one-shot override of the package
    target; it does not change the saved update channel. It is how the
    waiting period installs a release older than the advertised latest.
    """
    cmd = [str(config.openclaw_bin_path), "update", "--yes"]
    if target_version:
        cmd += ["--tag", target_version]
    label = "openclaw update" + (f" --tag {target_version}" if target_version else "")
    _LOG.info("updater: running %s", cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean_subprocess_env(),
            timeout=UPDATE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"{label} timed out after {UPDATE_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return f"{label} could not be invoked: {exc}"
    new_version = extract_version(proc.stdout) or extract_version(proc.stderr)
    if proc.returncode == 0:
        if new_version:
            return f"{label} exit 0 (reports {new_version})"
        return f"{label} exit 0"
    _LOG.error(
        "updater: %s exit %s\n"
        "----- stdout -----\n%s\n----- stderr -----\n%s\n------------------",
        label,
        proc.returncode,
        proc.stdout or "<empty>",
        proc.stderr or "<empty>",
    )
    return f"{label} exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}"


def run_post_update_hook(config: Config) -> str:
    """Run the operator's POST_UPDATE_HOOK after an install-modifying update.

    The hook exists so operator-approved local patches (applied on top of
    OpenClaw's installed files) survive a reinstall: `openclaw update`
    replaces the install wholesale, so anything patched into it must be
    re-applied afterwards. The hook is the operator's own script; Mechanic
    just runs it with the house subprocess hygiene and reports the result.
    A hook failure is loud in the report but does not fail the run; the
    post-update verify stays the authoritative health signal.
    """
    hook = config.post_update_hook
    if hook is None:
        return (
            "POST_UPDATE_HOOK not configured; local patches (if any) "
            "were NOT re-applied"
        )
    if not hook.is_file():
        _LOG.error("updater: POST_UPDATE_HOOK %s does not exist", hook)
        return f"FAILED: hook {hook} does not exist"
    if not os.access(hook, os.X_OK):
        _LOG.error("updater: POST_UPDATE_HOOK %s is not executable", hook)
        return f"FAILED: hook {hook} is not executable (chmod +x it)"

    timeout = config.post_update_hook_timeout_seconds
    _LOG.info("updater: running post-update hook %s", hook)
    try:
        proc = subprocess.run(
            [str(hook)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean_subprocess_env(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _LOG.error("updater: post-update hook timed out after %ss", timeout)
        return f"FAILED: hook timed out after {timeout}s"
    except OSError as exc:
        _LOG.error("updater: could not invoke post-update hook: %s", exc)
        return f"FAILED: could not invoke hook: {exc}"

    if proc.returncode == 0:
        _LOG.info("updater: post-update hook %s exit 0", hook.name)
        return f"{hook.name} exit 0 (local patches re-applied)"
    _LOG.error(
        "updater: post-update hook %s exit %s\n"
        "----- stdout -----\n%s\n----- stderr -----\n%s\n------------------",
        hook.name,
        proc.returncode,
        proc.stdout or "<empty>",
        proc.stderr or "<empty>",
    )
    return (
        f"FAILED: {hook.name} exit {proc.returncode}: "
        f"{(proc.stderr or proc.stdout).strip()[:200]}"
    )


def _emit_reports(config: Config, reports: list[RunReport], states: dict[str, SupervisorState]) -> None:
    rendered = format_reports(reports, states)
    _LOG.info("morning report:\n%s", rendered)
    subject = short_subject(reports)
    result = notify_send(config, subject, rendered)
    if not result.delivered and result.kind != "none":
        _LOG.warning("notifier did not deliver: %s", result.error)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    """Launchd-invoked entry point. `--test-fire` skips the update step."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="mechanic-updater",
        description="Mechanic nightly update routine.",
    )
    parser.add_argument(
        "--test-fire",
        action="store_true",
        help="Skip the update step but run everything else "
             "(snapshot, doctor, verify, last-known-good refresh, report). "
             "Used by install.sh to surface first-run macOS permission prompts.",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Run only this target (openclaw or hermes). Default: every target in TARGETS.",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip the nightly snapshot and the last-known-good refresh (the tars that are "
             "most of a run's wall-clock). Doctor, verify, gateway check and the report still "
             "run. NO rollback point is captured. For manual/developer runs only; the launchd "
             "nightly never sets this.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except Exception as exc:
        configure_logging(LOG_FILE, level="ERROR", secrets=())
        logging.getLogger(__name__).error("updater: could not load config: %s", exc)
        return 2

    configure_logging(
        LOG_FILE,
        level=config.log_level,
        secrets=config.secret_values(),
        also_stderr=args.test_fire or args.no_snapshot,
    )
    try:
        return run_updater(
            config, test_fire=args.test_fire, only=args.target, no_snapshot=args.no_snapshot
        )
    except ValueError as exc:
        logging.getLogger(__name__).error("updater: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
