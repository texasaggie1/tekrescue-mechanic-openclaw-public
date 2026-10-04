"""Target adapters: the products Mechanic looks after, behind one interface.

The nightly and the heartbeat do not know which product they are handling.
They ask a Target to probe health, assess whether tonight should install
anything, snapshot, apply, run doctor, verify, and describe itself. Two
adapters exist:

- OpenClawTarget: the original v0.1 behaviour, unchanged in substance.
  `openclaw update status --json` says whether an update exists,
  release_age.plan_update picks the newest npm version old enough,
  `openclaw update --yes --tag <v>` installs it, `openclaw doctor --fix
  --non-interactive` follows, POST_UPDATE_HOOK re-applies local patches.
- HermesTarget: hermes_release.scan_and_plan picks the newest release tag
  old enough and pins the git checkout to it (or runs `hermes update
  --yes` in hermes-update mode), `hermes pm install` rebuilds the
  environment, `hermes doctor [--fix]` follows, and the gateway is
  restarted through launchd only if it was running and the operator has
  not disabled it.

Both share the operator's off switches: a target not listed in TARGETS is
never constructed, and a target whose gateway the operator has
`launchctl disable`d is skipped entirely, CLI and all, because OpenClaw's
CLI brings its own gateway back when probed (SESSIONS.md 2026-10-04).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import daemon
from .config import Config, HermesSettings, OpenClawSettings
from .doctor_runner import DoctorError, UnknownPromptAbort, run_doctor as run_openclaw_doctor
from .release_age import UpdatePlan, find_npm, plan_update
from .rollback import SnapshotStore, hermes_store, openclaw_store
from .verifier import (
    UpdateAvailability,
    VerifyResult,
    check_openclaw_updates,
    extract_version,
    verify_binary,
)
from . import hermes_release


_LOG = logging.getLogger(__name__)


@dataclass
class Assessment:
    """Whether an update exists and what tonight does about it."""

    available: bool
    install: bool
    reason: str
    skip_summary: str
    error: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    plan: object = None
    advertised: Optional[str] = None

    @property
    def waiting_note(self) -> Optional[str]:
        if not self.available and not self.error:
            return None
        return self.reason


@dataclass
class UpdateOutcome:
    """Result of the install step. Verify, not this, decides health."""

    summary: str
    install_modified: bool
    success: bool
    notes: list[str] = field(default_factory=list)
    expected_code: Optional[str] = None
    gateway_restarted: bool = False
    gateway_note: Optional[str] = None


@dataclass
class DoctorOutcome:
    summary: str
    success: bool
    exit_code: Optional[int] = None
    aborted_prompt: Optional[str] = None


class Target:
    """Base adapter. Subclasses fill in the product specifics."""

    name: str = ""
    display_name: str = ""

    def __init__(self, config: Config) -> None:
        self.config = config

    # --- identity -------------------------------------------------------

    @property
    def bin_path(self) -> Path:
        raise NotImplementedError

    @property
    def min_update_age_days(self) -> int:
        raise NotImplementedError

    @property
    def service_label(self) -> str:
        return daemon.label_for(self.name)

    def disabled_by_operator(self) -> Optional[bool]:
        """True when the operator has `launchctl disable`d the gateway."""
        return daemon.is_disabled(self.service_label)

    def store(self) -> SnapshotStore:
        raise NotImplementedError

    # --- nightly steps --------------------------------------------------

    def probe(self) -> VerifyResult:
        return verify_binary(self.bin_path, label=self.display_name)

    def assess(self, health: VerifyResult) -> Assessment:
        raise NotImplementedError

    def pre_snapshot(self) -> Optional[str]:
        """Anything to do right before Mechanic's tar (a note for the report)."""
        return None

    def snapshot_extra(self) -> dict:
        return {}

    def apply(self, assessment: Assessment) -> UpdateOutcome:
        raise NotImplementedError

    #: Label for the post-doctor step in the morning report.
    post_install_label: str = "Hook"

    def post_install_hook(self, outcome: UpdateOutcome) -> Optional[str]:
        """Runs after doctor and before verify, only when the install changed."""
        return None

    def doctor(self) -> DoctorOutcome:
        raise NotImplementedError

    def verify(self, outcome: Optional[UpdateOutcome]) -> VerifyResult:
        return self.probe()

    # --- heartbeat ------------------------------------------------------

    def heartbeat_lines(self, health: VerifyResult, assessment: Optional[Assessment]) -> list[str]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------


class OpenClawTarget(Target):
    name = "openclaw"
    display_name = "OpenClaw"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        assert config.openclaw is not None
        self.settings: OpenClawSettings = config.openclaw

    @property
    def bin_path(self) -> Path:
        return self.settings.bin_path

    @property
    def min_update_age_days(self) -> int:
        return self.settings.min_update_age_days

    def store(self) -> SnapshotStore:
        return openclaw_store(self.config)

    def npm_path(self) -> Optional[Path]:
        return find_npm(self.config)

    def assess(self, health: VerifyResult) -> Assessment:
        availability: UpdateAvailability = check_openclaw_updates(self.bin_path)
        if availability.error is not None:
            reason = f"update check failed: {availability.error}; not touching the install"
            return Assessment(
                available=False, install=False, reason=reason,
                skip_summary=f"skipped ({reason})", error=availability.error,
            )
        if not availability.available:
            installed = health.version or "unknown"
            reason = f"no update available; installed {installed} is the registry latest"
            return Assessment(
                available=False, install=False, reason=reason,
                skip_summary=f"skipped ({reason})",
            )
        plan: UpdatePlan = plan_update(
            self.config, installed_version=health.version, availability=availability,
        )
        label = f"Waiting period (MIN_UPDATE_AGE_DAYS={self.min_update_age_days}): {plan.reason}"
        if plan.install:
            skip = ""
        elif plan.error:
            skip = "skipped (waiting period could not be applied; see notes)"
        else:
            skip = "skipped (waiting period; see notes)"
        return Assessment(
            available=True, install=plan.install, reason=label, skip_summary=skip,
            error=plan.error, notes=list(plan.notes), plan=plan,
            advertised=availability.latest_version,
        )

    def apply(self, assessment: Assessment) -> UpdateOutcome:
        from .updater import run_openclaw_update  # late import: updater imports targets

        plan: UpdatePlan = assessment.plan
        summary = run_openclaw_update(
            self.config, target_version=plan.target_version if plan.use_tag else None,
        )
        return UpdateOutcome(
            summary=summary, install_modified=True,
            success=" exit 0" in summary,
        )

    def post_install_hook(self, outcome: UpdateOutcome) -> Optional[str]:
        from .updater import run_post_update_hook

        return run_post_update_hook(self.config)

    def doctor(self) -> DoctorOutcome:
        try:
            result = run_openclaw_doctor(self.config)
        except UnknownPromptAbort as abort:
            _LOG.error("updater: %s", abort)
            return DoctorOutcome(
                summary=f"aborted on unknown prompt under {abort.mode}",
                success=False, aborted_prompt=abort.prompt_text,
            )
        except DoctorError as exc:
            _LOG.error("updater: doctor failed: %s", exc)
            return DoctorOutcome(summary=f"doctor failed: {exc}", success=False)
        if result.success:
            summary = (
                f"exit 0, matched {len(result.matched_prompts)} known prompt(s)"
                f"{', AUTO_YES answered ' + str(len(result.auto_yes_prompts)) if result.auto_yes_prompts else ''}"
            )
        else:
            summary = result.reason or "doctor failed without a reason"
        return DoctorOutcome(summary=summary, success=result.success, exit_code=result.exit_code)

    def heartbeat_lines(self, health: VerifyResult, assessment: Optional[Assessment]) -> list[str]:
        version = health.version or "unknown version"
        lines = [f"OpenClaw {version} healthy ({health.duration_ms} ms)."]
        if assessment is None:
            return lines
        if assessment.error and not assessment.available:
            lines.append(f"Update check skipped: {assessment.error}")
            return lines
        if not assessment.available:
            lines.append("No updates available.")
            return lines
        plan: UpdatePlan = assessment.plan
        latest = plan.target_version
        advertised = assessment.advertised or "newer version"
        if plan.install:
            if self.min_update_age_days <= 0:
                lines.append(
                    f"Update available: {advertised}. Mechanic will install at "
                    f"{self.config.update_time} local."
                )
            else:
                lines.append(
                    f"Update available: {advertised}. Mechanic will install {latest or advertised} at "
                    f"{self.config.update_time} local: {plan.reason}"
                )
        elif plan.error:
            lines.append(f"Update available: {advertised}, NOT installing. {plan.reason}")
        else:
            lines.append(f"Update available: {advertised}, waiting. {plan.reason}")
        return lines


# ---------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------


class HermesTarget(Target):
    name = "hermes"
    display_name = "Hermes"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        assert config.hermes is not None
        self.settings: HermesSettings = config.hermes
        self._gateway_was_running: Optional[bool] = None

    @property
    def bin_path(self) -> Path:
        return self.settings.bin_path

    @property
    def min_update_age_days(self) -> int:
        return self.settings.min_update_age_days

    def store(self) -> SnapshotStore:
        return hermes_store(self.config)

    def probe(self) -> VerifyResult:
        return verify_binary(self.bin_path, label="Hermes")

    def assess(self, health: VerifyResult) -> Assessment:
        plan = hermes_release.scan_and_plan(self.settings)
        label = (
            f"Waiting period (HERMES_MIN_UPDATE_AGE_DAYS={self.min_update_age_days}, "
            f"mode {plan.mode}): {plan.reason}"
        )
        notes = list(plan.notes)
        if plan.install:
            skip = ""
        elif plan.error:
            skip = "skipped (could not plan; see notes)"
        else:
            skip = "skipped (waiting period; see notes)"
        available = plan.install or plan.next_tag is not None
        return Assessment(
            available=available, install=plan.install, reason=label, skip_summary=skip,
            error=plan.error, notes=notes, plan=plan,
        )

    def pre_snapshot(self) -> Optional[str]:
        return hermes_release.quick_backup(self.settings)

    def snapshot_extra(self) -> dict:
        head = hermes_release.head_commit(self.settings)
        return {
            "code_sha": head,
            "code_label": hermes_release.describe_head(self.settings) if head else None,
            "source_dir": str(self.settings.source_dir),
        }

    def apply(self, assessment: Assessment) -> UpdateOutcome:
        plan: hermes_release.HermesPlan = assessment.plan
        # Remember the gateway's state before anything moves; the restart
        # decision is made after doctor (post_install_hook), as `hermes
        # update` orders it: code, dependencies, config migration, restart.
        self._gateway_was_running = daemon.is_loaded(self.service_label)

        if plan.mode == hermes_release.MODE_HERMES_UPDATE:
            result = hermes_release.apply_hermes_update(self.settings, plan)
            return UpdateOutcome(
                summary=result.summary, install_modified=True, success=result.success,
                notes=list(result.steps), expected_code=None,
            )

        result = hermes_release.apply_release_tag(self.settings, plan)
        return UpdateOutcome(
            summary=result.summary, install_modified=True, success=result.success,
            notes=list(result.steps),
            expected_code=plan.target_commit if result.success else result.code_after,
        )

    post_install_label = "Gateway"

    def post_install_hook(self, outcome: UpdateOutcome) -> Optional[str]:
        """Restart the gateway through launchd, after doctor has migrated config.

        Only when one was running before the update and the operator has
        not disabled it. In hermes-update mode Hermes restarted it itself.
        """
        plan_mode = getattr(outcome, "mode", None)
        if outcome.expected_code is None and not outcome.success:
            return "install failed; gateway left as it was"
        if self.settings.update_mode == hermes_release.MODE_HERMES_UPDATE:
            return "restarted by hermes update itself"
        label = self.service_label
        if not self._gateway_was_running:
            return "was not running before the update; not starting it"
        if daemon.is_disabled(label):
            return f"{label} is disabled by the operator; not restarting it"
        note = hermes_release.gateway_restart(self.settings)
        outcome.gateway_restarted = True
        outcome.gateway_note = note
        return note

    def doctor(self) -> DoctorOutcome:
        summary, ok = hermes_release.run_doctor(self.settings)
        return DoctorOutcome(summary=summary, success=ok)

    def verify(self, outcome: Optional[UpdateOutcome]) -> VerifyResult:
        health = self.probe()
        if not health.healthy:
            return health
        problems: list[str] = []
        if outcome is not None and outcome.expected_code:
            head = hermes_release.head_commit(self.settings)
            if head != outcome.expected_code:
                problems.append(
                    f"checkout is on {(head or 'unknown')[:12]}, expected "
                    f"{outcome.expected_code[:12]}"
                )
        if outcome is not None and outcome.gateway_restarted:
            ok, note = hermes_release.gateway_status_ok(self.settings)
            if not ok:
                problems.append(note)
        if outcome is not None and outcome.install_modified and outcome.success:
            ok, note = hermes_release.pm_status_ok(self.settings)
            if not ok:
                problems.append(note)
        if problems:
            return VerifyResult(
                healthy=False, version=health.version,
                reason="; ".join(problems), exit_code=health.exit_code,
                duration_ms=health.duration_ms, stdout_tail=health.stdout_tail,
                stderr_tail=health.stderr_tail,
            )
        return health

    def heartbeat_lines(self, health: VerifyResult, assessment: Optional[Assessment]) -> list[str]:
        version = _hermes_version_label(health)
        lines = [f"Hermes {version} healthy ({health.duration_ms} ms)."]
        if assessment is None:
            return lines
        plan: hermes_release.HermesPlan = assessment.plan
        if plan is None:
            return lines
        if plan.install:
            verb = "pin to" if plan.mode == hermes_release.MODE_RELEASE_TAG else "update after"
            lines.append(
                f"Release available: {plan.target_tag}. Mechanic will {verb} it at "
                f"{self.config.update_time} local: {plan.reason}"
            )
        elif plan.error:
            lines.append(f"Hermes update: NOT planning. {plan.reason}")
        else:
            lines.append(f"Hermes update: waiting. {plan.reason}")
        for note in plan.notes:
            lines.append(f"Note: {note}")
        return lines


def _hermes_version_label(health: VerifyResult) -> str:
    """`hermes --version` prints `Hermes Agent v2026.9.24+3.gabc (2026.9.24)`."""
    if health.version:
        return health.version
    return extract_version(health.stdout_tail or "") or "unknown version"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_targets(config: Config, only: Optional[str] = None) -> list[Target]:
    """The configured targets, in TARGETS order, optionally just one."""
    built: list[Target] = []
    for name in config.targets:
        if only and name != only:
            continue
        if name == "openclaw":
            built.append(OpenClawTarget(config))
        elif name == "hermes":
            built.append(HermesTarget(config))
    if only and not built:
        raise ValueError(f"{only!r} is not in TARGETS ({', '.join(config.targets)})")
    return built
