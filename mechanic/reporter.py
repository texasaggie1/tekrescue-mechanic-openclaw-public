"""Formats the morning report.

Takes the structured result of a nightly run and renders a short
human-readable summary for the log and the optional notifier. Two shapes:
the happy path ("here's what happened, OpenClaw is fine") and the paused
variant ("STATUS: PAUSED, here's why, here's how to resume"). Both share a
header so the operator can scan past it on good mornings and stop on bad
ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .state import SupervisorState


@dataclass
class RunReport:
    """Structured input to the morning report renderer.

    The updater fills this in as the routine progresses. Any field can stay
    at its default if that step did not run (for example, if doctor aborted
    before verify, `verify_summary` stays None).
    """

    started_at: str
    finished_at: str
    paused: bool = False
    pause_reason: Optional[str] = None
    paused_at: Optional[str] = None
    snapshot_id: Optional[str] = None
    openclaw_version_before: Optional[str] = None
    openclaw_version_after: Optional[str] = None
    update_summary: Optional[str] = None
    install_modified: bool = False
    hook_summary: Optional[str] = None
    doctor_summary: Optional[str] = None
    verify_summary: Optional[str] = None
    rollback_summary: Optional[str] = None
    overall: str = "unknown"
    notes: list[str] = field(default_factory=list)


def format_report(report: RunReport, state: SupervisorState) -> str:
    """Render the morning report as a single multi-line string."""
    lines: list[str] = []

    if report.paused:
        lines.append("STATUS: PAUSED")
        if report.pause_reason:
            lines.append(f"  reason: {report.pause_reason}")
        if report.paused_at:
            lines.append(f"  since: {report.paused_at}")
        lines.append("  resume with: mechanic resume")
        lines.append("")
    else:
        lines.append(f"STATUS: {report.overall.upper()}")
        lines.append("")

    lines.append(f"Run started:  {report.started_at}")
    lines.append(f"Run finished: {report.finished_at}")
    lines.append("")

    if report.openclaw_version_before or report.openclaw_version_after:
        before = report.openclaw_version_before or "unknown"
        after = report.openclaw_version_after or "unknown"
        lines.append(f"OpenClaw version: {before} -> {after}")
        lines.append("")

    if report.snapshot_id:
        lines.append(f"Snapshot taken: {report.snapshot_id}")
    if report.update_summary:
        lines.append(f"Update:   {report.update_summary}")
        lines.append(
            f"Install modified: {'yes' if report.install_modified else 'no'}"
        )
    if report.hook_summary:
        lines.append(f"Hook:     {report.hook_summary}")
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

    return "\n".join(lines).rstrip() + "\n"


def short_subject(report: RunReport) -> str:
    """One-line subject suitable for a notifier (email subject, Slack title)."""
    if report.paused:
        return f"tekRESCUE Mechanic: PAUSED ({report.pause_reason or 'no reason recorded'})"
    return f"tekRESCUE Mechanic: {report.overall}"
