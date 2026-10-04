"""Tests for the morning report renderer across one and several targets."""

from __future__ import annotations

import unittest

from mechanic.reporter import RunReport, format_report, format_reports, short_subject
from mechanic.state import SupervisorState


def report(target: str, **kw) -> RunReport:
    base = dict(
        started_at="2026-10-05T07:00:00+00:00", finished_at="2026-10-05T07:09:00+00:00",
        target=target, snapshot_id="2026-10-05T07-00-00Z", overall="success",
        update_summary="skipped (waiting period; see notes)", doctor_summary="exit 0",
        verify_summary="healthy (x, exit 0, 10 ms)",
    )
    base.update(kw)
    return RunReport(**base)


class ReporterTests(unittest.TestCase):
    def test_single_target_has_no_header(self) -> None:
        text = format_report(report("openclaw", version_before="2026.9.2", version_after="2026.9.2"), SupervisorState())
        self.assertTrue(text.startswith("STATUS: SUCCESS\n"))
        self.assertIn("OpenClaw version: 2026.9.2 -> 2026.9.2", text)
        self.assertNotIn("==", text)

    def test_multi_target_blocks_and_subject(self) -> None:
        reports = [
            report("openclaw"),
            report("hermes", overall="failed", rollback_summary="NOT auto-rolled back. To revert: mechanic restore --target hermes 2026-10-05T07-00-00Z"),
        ]
        states = {"openclaw": SupervisorState(), "hermes": SupervisorState(consecutive_failures=1)}
        text = format_reports(reports, states)
        self.assertIn("== OpenClaw ==\nSTATUS: SUCCESS", text)
        self.assertIn("== Hermes ==\nSTATUS: FAILED", text)
        self.assertIn("Consecutive failures: 1", text)
        self.assertEqual(short_subject(reports), "tekRESCUE Mechanic: OpenClaw success, Hermes failed")

    def test_paused_block_names_the_target_resume_command(self) -> None:
        text = format_reports(
            [report("hermes", paused=True, pause_reason="3 consecutive verify failures", paused_at="2026-10-05T07:00:00Z")],
            {"hermes": SupervisorState(paused=True)},
        )
        self.assertIn("resume with: mechanic resume --target hermes", text)
        self.assertEqual(short_subject([report("hermes", paused=True, pause_reason="x")]), "tekRESCUE Mechanic: PAUSED (x)")


if __name__ == "__main__":
    unittest.main()
