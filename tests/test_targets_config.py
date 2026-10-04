"""Tests for TARGETS configuration, the launchd disabled guard, and the
OpenClaw planner's skip-list and deprecation gates.

Run with: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mechanic import daemon
from mechanic.config import ConfigError, load_config
from mechanic.release_age import plan_update
from mechanic.verifier import UpdateAvailability

from test_release_age import SNAPSHOT, make_config


MISSING_ENV = Path("/nonexistent/mechanic.env")

BASE_ENV = {
    "OPENCLAW_BIN_PATH": "/fake/openclaw",
    "OPENCLAW_CONFIG_PATH": "/fake/.openclaw",
}


def load_with(env: dict[str, str]):
    cleared = {k: "" for k in (
        "TARGETS", "OPENCLAW_BIN_PATH", "OPENCLAW_CONFIG_PATH", "HERMES_BIN_PATH",
        "HERMES_HOME", "HERMES_SOURCE_DIR", "HERMES_UPDATE_MODE", "HERMES_UPDATE_CHANNEL",
        "HERMES_MIN_UPDATE_AGE_DAYS", "HERMES_SKIP_TAGS", "HERMES_DOCTOR_FIX",
        "OPENCLAW_MIN_UPDATE_AGE_DAYS", "OPENCLAW_SKIP_VERSIONS", "MIN_UPDATE_AGE_DAYS",
    )}
    with mock.patch.dict(os.environ, {**cleared, **env}, clear=False):
        return load_config(MISSING_ENV)


class TargetsConfigTests(unittest.TestCase):
    def test_default_is_openclaw_only(self) -> None:
        config = load_with(BASE_ENV)
        self.assertEqual(config.targets, ("openclaw",))
        self.assertIsNotNone(config.openclaw)
        self.assertIsNone(config.hermes)
        self.assertEqual(config.openclaw.min_update_age_days, 7)
        self.assertEqual(config.openclaw_bin_path, Path("/fake/openclaw"))

    def test_hermes_only_needs_no_openclaw_paths(self) -> None:
        config = load_with({"TARGETS": "hermes"})
        self.assertEqual(config.targets, ("hermes",))
        self.assertIsNone(config.openclaw)
        self.assertEqual(config.hermes.bin_path, Path.home() / ".local" / "bin" / "hermes")
        self.assertEqual(config.hermes.home, Path.home() / ".hermes")
        self.assertEqual(config.hermes.source_dir, Path.home() / ".hermes" / "hermes-agent")
        self.assertEqual(config.hermes.update_mode, "release-tag")
        self.assertTrue(config.hermes.doctor_fix)
        with self.assertRaises(ConfigError):
            _ = config.openclaw_bin_path

    def test_both_targets_with_overrides(self) -> None:
        config = load_with({
            **BASE_ENV,
            "TARGETS": "openclaw, hermes",
            "MIN_UPDATE_AGE_DAYS": "10",
            "HERMES_MIN_UPDATE_AGE_DAYS": "14",
            "HERMES_SKIP_TAGS": "v2026.9.21,v2026.9.14",
            "HERMES_UPDATE_MODE": "hermes-update",
            "HERMES_UPDATE_CHANNEL": "stable",
            "HERMES_DOCTOR_FIX": "false",
            "HERMES_SOURCE_DIR": "/src/hermes-agent",
            "OPENCLAW_SKIP_VERSIONS": "2026.9.6",
        })
        self.assertEqual(config.targets, ("openclaw", "hermes"))
        self.assertEqual(config.openclaw.min_update_age_days, 10)
        self.assertEqual(config.openclaw.skip_versions, ("2026.9.6",))
        self.assertEqual(config.hermes.min_update_age_days, 14)
        self.assertEqual(config.hermes.skip_tags, ("v2026.9.21", "v2026.9.14"))
        self.assertEqual(config.hermes.update_mode, "hermes-update")
        self.assertEqual(config.hermes.update_channel, "stable")
        self.assertFalse(config.hermes.doctor_fix)
        self.assertEqual(config.hermes.source_dir, Path("/src/hermes-agent"))

    def test_openclaw_in_targets_still_requires_its_paths(self) -> None:
        with self.assertRaises(ConfigError):
            load_with({"TARGETS": "openclaw,hermes"})

    def test_unknown_target_and_bad_mode_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_with({**BASE_ENV, "TARGETS": "openclaw,claude"})
        with self.assertRaises(ConfigError):
            load_with({"TARGETS": "hermes", "HERMES_UPDATE_MODE": "yolo"})


class DisabledGuardTests(unittest.TestCase):
    SAMPLE = (
        "disabled services = {\n"
        '\t"com.apple.something" => enabled\n'
        '\t"ai.openclaw.gateway" => disabled\n'
        '\t"ai.hermes.gateway" => enabled\n'
        "}\n"
    )

    def test_parse_disabled(self) -> None:
        parsed = daemon.parse_disabled(self.SAMPLE)
        self.assertTrue(parsed["ai.openclaw.gateway"])
        self.assertFalse(parsed["ai.hermes.gateway"])
        self.assertNotIn("ai.unknown", parsed)

    def test_is_disabled_reads_launchctl(self) -> None:
        completed = mock.Mock(returncode=0, stdout=self.SAMPLE)
        with mock.patch("mechanic.daemon.subprocess.run", return_value=completed):
            self.assertTrue(daemon.is_disabled("ai.openclaw.gateway"))
            self.assertFalse(daemon.is_disabled("ai.hermes.gateway"))
            self.assertFalse(daemon.is_disabled("ai.nothing"))

    def test_is_disabled_is_unknown_without_launchctl(self) -> None:
        with mock.patch("mechanic.daemon.subprocess.run", side_effect=OSError("no launchctl")):
            self.assertIsNone(daemon.is_disabled("ai.openclaw.gateway"))

    def test_labels(self) -> None:
        self.assertEqual(daemon.label_for("openclaw"), "ai.openclaw.gateway")
        self.assertEqual(daemon.label_for("hermes"), "ai.hermes.gateway")
        with self.assertRaises(daemon.DaemonControlError):
            daemon.label_for("other")


class OpenClawGatesTests(unittest.TestCase):
    NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    def plan(self, *, skip=(), deprecated=None, installed="2026.7.30"):
        return plan_update(
            make_config(7, skip=skip),
            installed_version=installed,
            availability=UpdateAvailability(available=True, latest_version="2026.9.4", error=None),
            now=self.NOW,
            snapshot=SNAPSHOT,
            deprecation_check=lambda v: (deprecated or {}).get(v),
        )

    def test_skip_list_falls_through_to_the_next_eligible(self) -> None:
        # Eligible on 09-12: 2026.9.1 (newest), 2026.8.2, 2026.8.1, ...
        plan = self.plan(skip=("2026.9.1",))
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_version, "2026.8.2")
        self.assertIn("2026.9.1 skipped (OPENCLAW_SKIP_VERSIONS)", plan.notes)

    def test_deprecated_falls_through_to_the_next_eligible(self) -> None:
        plan = self.plan(deprecated={"2026.9.1": "pulled, see advisory"})
        self.assertEqual(plan.target_version, "2026.8.2")
        self.assertTrue(any("deprecated on npm" in note for note in plan.notes))

    def test_installed_ahead_of_fallback_means_wait(self) -> None:
        plan = self.plan(skip=("2026.9.1", "2026.8.2"), installed="2026.8.2")
        self.assertFalse(plan.install)
        self.assertIn("Installed 2026.8.2 is newer than 2026.8.1", plan.reason)


if __name__ == "__main__":
    unittest.main()
