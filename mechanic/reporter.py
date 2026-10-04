"""Formats the morning report.

Takes the structured result of a nightly run, one RunReport per target,
and renders a short human-readable summary for the log and the optional
notifier. Each target gets the same shape: the happy path ("here's what
happened, it is fine") or the paused variant ("STATUS: PAUSED, here's why,
here's how to resume"). With one target the output is exactly the v0.1
report; with more, each target's block carries a header line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .state import SupervisorState


DISPLAY_NAMES = {"openclaw": "OpenClaw", "hermes": "Hermes"}


@dataclass
class RunReport:
    """Structured input to the morning report renderer, for one target.

    The updater fills this in as the routine progresses. Any field can stay
    at its default if that step did not run (for example, if doctor aborted
    before verify, `verify_summary` stays None).
    """

    started_at: str
    finished_at: str
    target: str = "openclaw"
    paused: bool = False
    pause_reason: Optional[str] = None
    paused_at: Optional[str] = None
    snapshot_id: Optional[str] = None
    version_before: Optional[str] = None
    version_after: Optional[str] = None
    update_summary: Optional[str] = None
    install_modified: bool = False
    hook_summary: Optional[str] = None
    hook_label: str = "Hook"
    doctor_summary: Optional[str] = None
    verify_summary: Optional[str] = None
    rollback_summary: Optional[str] = None
    overall: str = "unknown"
    notes: list[str] = field(default_factory=list)

    # Pre-v0.2 names, kept for callers and docs.
    @property
    def openclaw_version_before(self) -> Optional[str]:
        return self.version_before

    @property
    def openclaw_version_after(self) -> Optional[str]:
        return self.version_after

    @property
    def display_name(self) -> str:
        return DISPLAY_NAMES.get(self.target, self.target)


def format_report(report: RunReport, state: SupervisorState) -> str:
    """Render one target's report (the v0.1 single-target shape)."""
    return format_reports([report], {report.target: state})


def format_reports(reports: list[RunReport], states: dict[str, SupervisorState]) -> str:
    """Render the morning report for every target as one multi-line string."""
    blocks: list[str] = []
    multi = len(reports) > 1
    for report in reports:
        state = states.get(report.target, SupervisorState())
        lines: list[str] = []
        if multi:
            lines.append(f"== {report.display_name} ==")
        lines.extend(_target_lines(report, state))
        blocks.append("\n".join(lines).rstrip())
    return "\n\n".join(blocks).rstrip() + "\n"


def _target_lines(report: RunReport, state: SupervisorState) -> list[str]:
    lines: list[str] = []
    resume = (
        "mechanic resume" if report.target == "openclaw"
        else f"mechanic resume --target {report.target}"
    )

    if report.paused:
        lines.append("STATUS: PAUSED")
        if report.pause_reason:
            lines.append(f"  reason: {report.pause_reason}")
        if report.paused_at:
            lines.append(f"  since: {report.paused_at}")
        lines.append(f"  resume with: {resume}")
        lines.append("")
    else:
        lines.append(f"STATUS: {report.overall.upper()}")
        lines.append("")

    lines.append(f"Run started:  {report.started_at}")
    lines.append(f"Run finished: {report.finished_at}")
    lines.append("")

    if report.version_before or report.version_after:
        before = report.version_before or "unknown"
        after = report.version_after or "unknown"
        lines.append(f"{report.display_name} version: {before} -> {after}")
        lines.append("")

    if report.snapshot_id:
        lines.append(f"Snapshot taken: {report.snapshot_id}")
    if report.update_summary:
        lines.append(f"Update:   {report.update_summary}")
        lines.append(
            f"Install modified: {'yes' if report.install_modified else 'no'}"
        )
    if report.hook_summary:
        lines.append(f"{(report.hook_label + ':').ljust(9)} {report.hook_summary}")
    if report.doctor_summary:
        lines.append(f"Doctor:   {report.doctor_summary}")
    if report.verify_summary:
        lines.append(f"Verify:   {report.verify_summary}")
    if report.rollback_summary:
        lines.append(f"Rollback: {report.rollback_summary}")

    if state.consecutive_failures:
        lines.append("")
        lines.append(f"Consecutive failures: {state.consecutive_failures}")
    if state.last_success:
        lines.append(f"Last success: {state.last_success}")
    if state.last_known_good_snapshot_id:
        lines.append(f"Last known good: {state.last_known_good_snapshot_id}")

    if report.notes:
        lines.append("")
        lines.append("Notes:")
        for note in report.notes:
            lines.append(f"  - {note}")
    return lines


def short_subject(reports: list[RunReport] | RunReport) -> str:
    """One-line subject suitable for a notifier (email subject, Slack title)."""
    if isinstance(reports, RunReport):
        reports = [reports]
    if not reports:
        return "tekRESCUE Mechanic: no targets"
    if len(reports) == 1:
        report = reports[0]
        if report.paused:
            return f"tekRESCUE Mechanic: PAUSED ({report.pause_reason or 'no reason recorded'})"
        return f"tekRESCUE Mechanic: {report.overall}"
    parts = []
    for report in reports:
        status = "PAUSED" if report.paused else report.overall
        parts.append(f"{report.display_name} {status}")
    return "tekRESCUE Mechanic: " + ", ".join(parts)
