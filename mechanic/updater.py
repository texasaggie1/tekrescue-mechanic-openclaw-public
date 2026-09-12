"""Nightly update routine (v0.1.4).

Composes the steps documented in CLAUDE.md section 5:

  0. Read supervisor state. If paused, log the reason and exit immediately.
  1. Pre-flight disk check.
  2. Pre-update verify. If OpenClaw is already broken, skip the update and
     run doctor anyway in case it can fix the existing breakage.
  3. Capture a nightly snapshot as a tar.gz archive (see rollback.py).
  4. Update availability gate (new in v0.1.2): probe `openclaw update
     status --json` and only proceed to the install when the registry has
     a version newer than the installed one. `openclaw update --yes`
     reinstalls the SAME version when nothing is newer, replacing every
     file in dist/ and silently wiping operator-approved local patches
     (found 2026-08-19 on the Mac Mini: nightly 02:05 mtimes on dist/
     with an unchanged version, and the dreaming runtime override gone
     every morning). If the probe errors, we also skip: not touching the
     install is the safe default, and the supervisor heartbeat surfaces
     the same probe error six times a day.
  4b. Release waiting period (new in v0.1.4): when an update exists, ask
     release_age.plan_update which version, if any, has been public on the
     npm registry for MIN_UPDATE_AGE_DAYS (default 7). OpenClaw releases
     every two or three days, so the plan usually names an older release
     than the one OpenClaw advertises, installed with `--tag`. Anything the
     plan cannot establish (npm missing, registry unreachable) resolves to
     "leave the install alone" and is spelled out in the morning report.
  5. When the gate passes, run `openclaw update --yes` (plus `--tag
     <version>` when the plan pins one) via subprocess.run with a clean
     env and stdin closed (CLAUDE.md section 8 hygiene).
  6. Run `openclaw doctor --fix --non-interactive`, also via subprocess.run.
     PROMPT_MODE and KNOWN_PROMPTS are dormant under --non-interactive and
     reserved for v0.2 if/when we drop the flag.
  7. Post-update hook (new in v0.1.2): whenever step 5 actually invoked
     `openclaw update`, run the operator's POST_UPDATE_HOOK script so
     approved local patches are re-applied on top of the fresh install.
     Runs before post-verify so a hook that breaks OpenClaw is caught.
  8. Post-update verify. **This is the authoritative health signal**;
     doctor's exit code is informational only.
  9. On verify failure: log + notify, increment consecutive_failures,
     auto-pause at MAX_CONSECUTIVE_FAILURES. **NO auto-rollback.** The
     morning report tells the operator: `mechanic restore <snapshot-id>`.
 10. On verify success: reset consecutive_failures, stamp last_success,
     and refresh last-known-good as a fresh tar.gz.
 11. Prune nightlies down to SNAPSHOT_RETENTION_DAYS entries.
 12. Write the morning report to the log and the configured notifier. The
     report always states whether the install was modified this run.

Entry points: `run_updater(config)` returns 0 healthy, 1 soft-fail (verify
failed but Mechanic stayed up), 2 hard-fail (paused or pre-flight
aborted). `main()` is the launchd-invoked CLI wrapper.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from .config import Config, LOG_FILE, clean_subprocess_env, load_config
from .doctor_runner import (
    DoctorError,
    DoctorResult,
    UnknownPromptAbort,
    run_doctor,
)
from .logging_setup import configure_logging
from .notifier import send as notify_send
from .release_age import plan_update
from .reporter import RunReport, format_report, short_subject
from .rollback import (
    InsufficientDiskError,
    KIND_LAST_KNOWN_GOOD,
    KIND_NIGHTLY,
    RollbackError,
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
from .verifier import (
    VerifyResult,
    check_for_updates,
    extract_version,
    verify_openclaw,
)


_LOG = logging.getLogger(__name__)

UPDATE_TIMEOUT_SECONDS = 600


def run_updater(config: Config, *, test_fire: bool = False) -> int:
    """Execute the nightly routine. Returns a shell-style exit code.

    When `test_fire=True`, step 4 (`openclaw update --yes`) is skipped. Every
    other step runs as normal: snapshot, doctor, post-verify, last-known-good
    refresh, prune, morning report. The report includes a clear "TEST FIRE"
    annotation so the operator (or notifier recipient) understands the
    openclaw update step was deliberately not exercised. Used by
    install.sh to surface any first-run macOS permission prompts in
    foreground while the operator is still at the terminal.
    """
    started_at = _now_iso()
    state = load_state()

    if state.paused:
        _LOG.warning(
            "updater: paused (%s since %s); exiting without running",
            state.pause_reason,
            state.paused_at,
        )
        report = RunReport(
            started_at=started_at,
            finished_at=_now_iso(),
            paused=True,
            pause_reason=state.pause_reason,
            paused_at=state.paused_at,
            notes=["Updater exited because supervisor state is paused."],
        )
        _emit_report(config, report, state)
        return 2

    state = record_run_start(state)
    save_state(state)

    # 1. Pre-flight disk check. May emergency-prune the oldest nightly
    # snapshots to make room for this run; see ensure_disk_headroom.
    try:
        emergency_pruned = ensure_disk_headroom(config)
    except InsufficientDiskError as exc:
        _LOG.error("updater: %s", exc)
        report = RunReport(
            started_at=started_at,
            finished_at=_now_iso(),
            overall="aborted",
            notes=[f"Pre-flight disk check failed: {exc}"],
        )
        _emit_report(config, report, state)
        return 2

    # 2. Pre-update verify.
    pre_verify = verify_openclaw(config)
    version_before = pre_verify.version
    openclaw_was_broken = not pre_verify.healthy
    if openclaw_was_broken:
        _LOG.warning(
            "updater: OpenClaw is already broken pre-update (%s); "
            "skipping update step, running doctor anyway",
            pre_verify.short_summary(),
        )

    # 3. Snapshot.
    try:
        snapshot = capture_snapshot(
            config,
            kind=KIND_NIGHTLY,
            openclaw_version=version_before,
            verified_healthy=pre_verify.healthy,
        )
    except RollbackError as exc:
        _LOG.error("updater: snapshot capture failed: %s", exc)
        report = RunReport(
            started_at=started_at,
            finished_at=_now_iso(),
            overall="aborted",
            notes=[f"Snapshot capture failed: {exc}"],
        )
        _emit_report(config, report, state)
        return 2

    # 4 + 5. Update, gated on actual availability. `openclaw update --yes`
    # reinstalls the same version when nothing newer exists, which replaces
    # every file in dist/ and wipes operator-applied local patches, so we
    # never invoke it unless the registry really has something new.
    install_modified = False
    waiting_note: Optional[str] = None
    if test_fire:
        update_summary = "skipped (--test-fire; openclaw update not invoked)"
    elif openclaw_was_broken:
        update_summary = "skipped (OpenClaw already broken pre-update)"
    else:
        availability = check_for_updates(config)
        if availability.error is not None:
            update_summary = (
                f"skipped (update check failed: {availability.error}; "
                f"not touching the install)"
            )
            _LOG.warning("updater: %s", update_summary)
        elif not availability.available:
            installed = version_before or "unknown"
            update_summary = (
                f"skipped (no update available; installed {installed} "
                f"is the registry latest)"
            )
            _LOG.info("updater: %s", update_summary)
        else:
            plan = plan_update(
                config,
                installed_version=version_before,
                availability=availability,
            )
            waiting_note = (
                f"Waiting period (MIN_UPDATE_AGE_DAYS="
                f"{config.min_update_age_days}): {plan.reason}"
            )
            if not plan.install:
                if plan.error:
                    update_summary = "skipped (waiting period could not be applied; see notes)"
                    _LOG.warning("updater: %s", plan.reason)
                else:
                    update_summary = "skipped (waiting period; see notes)"
                    _LOG.info("updater: %s", plan.reason)
            else:
                _LOG.info(
                    "updater: update available (%s -> %s); %s; running openclaw update",
                    version_before or "unknown",
                    plan.target_version or availability.latest_version or "unknown",
                    plan.reason,
                )
                update_summary = _run_openclaw_update(
                    config,
                    target_version=plan.target_version if plan.use_tag else None,
                )
                install_modified = True

    # 6. Doctor.
    doctor_summary, doctor_result, doctor_aborted_prompt = _run_doctor_step(config)

    # 7. Post-update hook: re-apply operator-approved local patches after
    # any run that invoked `openclaw update`. Runs before post-verify so a
    # hook that breaks OpenClaw is caught by the authoritative signal.
    hook_summary: Optional[str] = None
    if install_modified:
        hook_summary = _run_post_update_hook(config)

    # 8. Post-update verify.
    post_verify = verify_openclaw(config)
    verify_summary = post_verify.short_summary()
    version_after = post_verify.version

    # Post-verify is the authoritative signal: if OpenClaw answers a probe
    # cleanly after the routine, OpenClaw is healthy. Doctor exit codes go
    # in the report for visibility but do not override the verifier; doctor
    # can exit non-zero for warnings ("found things I couldn't fully fix")
    # while OpenClaw itself remains operational.
    healthy = post_verify.healthy and doctor_aborted_prompt is None
    doctor_warning = (
        doctor_result is not None
        and not doctor_result.success
        and doctor_aborted_prompt is None
    )

    rollback_summary: Optional[str] = None
    if healthy:
        # Success path.
        try:
            lkg = capture_snapshot(
                config,
                kind=KIND_LAST_KNOWN_GOOD,
                openclaw_version=version_after,
                verified_healthy=True,
            )
            state = record_success(state, snapshot_id=lkg.snapshot_id)
        except RollbackError as exc:
            _LOG.warning("updater: could not refresh last-known-good: %s", exc)
            state = record_success(state, snapshot_id=snapshot.snapshot_id)
        overall = "success"
    else:
        # v0.1.1 failure path: log + notify, no auto-rollback. OpenClaw is a
        # live daemon, so rolling back its config dir requires stopping the
        # daemon, and that is too risky for an unattended nightly. Operator
        # decides: `mechanic restore <snapshot-id>` does the daemon dance.
        _LOG.warning(
            "updater: verify failed; NOT auto-rolling back. "
            "Run `mechanic restore %s` to revert.",
            snapshot.snapshot_id,
        )
        rollback_summary = (
            f"NOT auto-rolled back. To revert: mechanic restore {snapshot.snapshot_id}"
        )
        state = record_failure(
            state, max_consecutive_failures=config.max_consecutive_failures
        )
        overall = "failed"

    # 11. Prune.
    pruned = prune_nightlies(config)
    if pruned:
        _LOG.info("updater: pruned %d nightly snapshots", len(pruned))

    save_state(state)

    # 12. Morning report.
    notes: list[str] = []
    if emergency_pruned:
        notes.append(
            f"Disk: emergency-pruned {len(emergency_pruned)} oldest nightly "
            f"snapshot(s) to make room for this run: "
            f"{', '.join(emergency_pruned)}"
        )
    if test_fire:
        notes.append(
            "TEST FIRE: this run was triggered by install.sh to surface any "
            "first-run macOS permission prompts. The openclaw update step was "
            "deliberately skipped; everything else ran normally."
        )
    if doctor_aborted_prompt:
        notes.append(
            f"Doctor aborted on unrecognised prompt: {doctor_aborted_prompt!r}. "
            f"Run `mechanic capture-prompts` to teach Mechanic the answer."
        )
    if openclaw_was_broken:
        notes.append("OpenClaw was already unhealthy pre-update; update step was skipped.")
    if waiting_note:
        notes.append(waiting_note)
    if doctor_warning:
        notes.append(
            f"Doctor exited non-zero ({doctor_result.exit_code}) but OpenClaw "
            f"verify still passed. This usually means doctor reported warnings "
            f"it could not auto-fix. Review with: openclaw doctor --lint"
        )

    report = RunReport(
        started_at=started_at,
        finished_at=_now_iso(),
        paused=state.paused,
        pause_reason=state.pause_reason,
        paused_at=state.paused_at,
        snapshot_id=snapshot.snapshot_id,
        openclaw_version_before=version_before,
        openclaw_version_after=version_after,
        update_summary=update_summary,
        install_modified=install_modified,
        hook_summary=hook_summary,
        doctor_summary=doctor_summary,
        verify_summary=verify_summary,
        rollback_summary=rollback_summary,
        overall=overall,
        notes=notes,
    )
    _emit_report(config, report, state)

    if state.paused:
        return 2
    if not healthy:
        return 1
    return 0


def _run_openclaw_update(config: Config, *, target_version: Optional[str] = None) -> str:
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


def _run_post_update_hook(config: Config) -> str:
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


def _run_doctor_step(
    config: Config,
) -> tuple[str, Optional[DoctorResult], Optional[str]]:
    try:
        result = run_doctor(config)
    except UnknownPromptAbort as abort:
        _LOG.error("updater: %s", abort)
        return (
            f"aborted on unknown prompt under {abort.mode}",
            None,
            abort.prompt_text,
        )
    except DoctorError as exc:
        _LOG.error("updater: doctor failed: %s", exc)
        return (f"doctor failed: {exc}", None, None)

    if result.success:
        summary = (
            f"exit 0, matched {len(result.matched_prompts)} known prompt(s)"
            f"{', AUTO_YES answered ' + str(len(result.auto_yes_prompts)) if result.auto_yes_prompts else ''}"
        )
    else:
        summary = result.reason or "doctor failed without a reason"
    return summary, result, None


def _emit_report(config: Config, report: RunReport, state: SupervisorState) -> None:
    rendered = format_report(report, state)
    _LOG.info("morning report:\n%s", rendered)
    subject = short_subject(report)
    result = notify_send(config, subject, rendered)
    if not result.delivered and result.kind != "none":
        _LOG.warning("notifier did not deliver: %s", result.error)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    """Launchd-invoked entry point. `--test-fire` skips the openclaw update step."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="mechanic-updater",
        description="Mechanic nightly update routine.",
    )
    parser.add_argument(
        "--test-fire",
        action="store_true",
        help="Skip the `openclaw update` step but run everything else "
             "(snapshot, doctor, verify, last-known-good refresh, report). "
             "Used by install.sh to surface first-run macOS permission prompts.",
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
        also_stderr=args.test_fire,
    )
    return run_updater(config, test_fire=args.test_fire)


if __name__ == "__main__":
    raise SystemExit(main())
