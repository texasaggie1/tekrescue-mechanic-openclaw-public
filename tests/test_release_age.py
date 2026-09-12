"""Tests for the release waiting period (mechanic/release_age.py).

Run with: python3 -m unittest discover -s tests -v

The registry fixture is a trimmed copy of the real `npm view openclaw time
versions dist-tags --json` output captured 2026-09-12. It keeps the
September 2026 release train, the 2026.6.35 extended-stable backport that
was published in the middle of it, a beta, and a hotfix-style
prerelease, because those are exactly the shapes the planner must get
right.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mechanic.config import Config, NotifierSettings
from mechanic.release_age import (
    RegistrySnapshot,
    UpdatePlan,
    is_prerelease,
    parse_registry_payload,
    plan_update,
    version_key,
)
from mechanic.verifier import UpdateAvailability, _extract_channel


REGISTRY_PAYLOAD = {
    "time": {
        "created": "2026-01-29T11:08:11.861Z",
        "modified": "2026-09-11T04:44:07.210Z",
        "2026.2.2": "2026-02-04T00:56:41.932Z",
        "2026.2.2-1": "2026-02-04T01:27:05.304Z",
        "2026.7.30": "2026-08-01T12:00:00.000Z",
        "2026.8.1": "2026-08-31T02:45:39.923Z",
        "2026.8.2": "2026-09-01T16:19:50.535Z",
        "2026.9.1-beta.1": "2026-08-28T20:04:30.859Z",
        "2026.9.1": "2026-09-03T18:03:45.738Z",
        "2026.9.2": "2026-09-05T19:13:07.937Z",
        "2026.9.3": "2026-09-08T13:06:36.195Z",
        "2026.6.35": "2026-09-10T04:51:20.511Z",
        "2026.9.4": "2026-09-11T02:44:59.212Z",
        "2026.9.9": "2026-09-01T00:00:00.000Z",
    },
    "versions": [
        "2026.2.2",
        "2026.2.2-1",
        "2026.7.30",
        "2026.8.1",
        "2026.8.2",
        "2026.9.1-beta.1",
        "2026.9.1",
        "2026.9.2",
        "2026.9.3",
        "2026.6.35",
        "2026.9.4",
        "2026.9.9",
    ],
    "dist-tags": {
        "beta": "2026.9.4",
        "extended-stable": "2026.6.35",
        "latest": "2026.9.4",
    },
}
# 2026.9.9 above is a deliberately odd entry: an old publish date but a
# version number ABOVE the `latest` dist-tag. The planner must ignore it
# (never install past what npm tags as latest).

SNAPSHOT, _ = parse_registry_payload(REGISTRY_PAYLOAD, npm_path=Path("/fake/npm"))
assert SNAPSHOT is not None

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def make_config(min_age_days: int = 7) -> Config:
    return Config(
        openclaw_bin_path=Path("/fake/openclaw"),
        openclaw_config_path=Path("/fake/.openclaw"),
        update_time="02:00",
        min_update_age_days=min_age_days,
        supervisor_interval_minutes=240,
        prompt_mode="STRICT",
        snapshot_retention_days=14,
        min_free_disk_mb_for_snapshot=500,
        max_consecutive_failures=3,
        pause_on_rollback_failure=True,
        post_update_hook=None,
        post_update_hook_timeout_seconds=300,
        log_level="INFO",
        notifier=NotifierSettings(kind="none"),
    )


def available(latest: str = "2026.9.4", channel: str | None = None) -> UpdateAvailability:
    return UpdateAvailability(
        available=True, latest_version=latest, error=None, channel=channel
    )


class VersionKeyTests(unittest.TestCase):
    def test_numeric_ordering(self) -> None:
        self.assertLess(version_key("2026.8.2"), version_key("2026.9.1"))
        self.assertLess(version_key("2026.9.4"), version_key("2026.10.1"))
        self.assertLess(version_key("2026.6.35"), version_key("2026.9.1"))

    def test_prerelease_sorts_below_release(self) -> None:
        self.assertLess(version_key("2026.9.1-beta.1"), version_key("2026.9.1"))
        self.assertLess(version_key("2026.2.2-1"), version_key("2026.2.2"))
        self.assertGreater(version_key("2026.9.5-beta.1"), version_key("2026.9.4"))

    def test_prerelease_detection(self) -> None:
        self.assertTrue(is_prerelease("2026.9.1-beta.1"))
        self.assertTrue(is_prerelease("2026.2.2-1"))
        self.assertFalse(is_prerelease("2026.9.4"))
        self.assertFalse(is_prerelease("2026.9.4+build.7"))

    def test_garbage_does_not_raise(self) -> None:
        self.assertEqual(version_key("nonsense"), ((0, 0, 0), 1, ()))


class ParseRegistryPayloadTests(unittest.TestCase):
    def test_only_versions_with_dates_count(self) -> None:
        self.assertEqual(SNAPSHOT.latest, "2026.9.4")
        self.assertEqual(len(SNAPSHOT.published), 12)
        self.assertNotIn("created", SNAPSHOT.published)
        self.assertEqual(
            SNAPSHOT.published["2026.9.4"],
            datetime(2026, 9, 11, 2, 44, 59, 212000, tzinfo=timezone.utc),
        )

    def test_single_version_string_is_accepted(self) -> None:
        snapshot, error = parse_registry_payload(
            {
                "time": {"1.0.0": "2026-01-01T00:00:00Z"},
                "versions": "1.0.0",
                "dist-tags": {"latest": "1.0.0"},
            },
            npm_path=Path("/fake/npm"),
        )
        self.assertIsNone(error)
        self.assertEqual(list(snapshot.published), ["1.0.0"])

    def test_malformed_payloads_become_errors(self) -> None:
        for payload in ([], {"versions": ["1.0.0"]}, {"time": {}, "versions": []}):
            snapshot, error = parse_registry_payload(payload, npm_path=Path("/fake/npm"))
            self.assertIsNone(snapshot)
            self.assertTrue(error)


class StableChannelPlanTests(unittest.TestCase):
    def plan(self, installed: str | None, now: datetime = NOW, min_age: int = 7) -> UpdatePlan:
        return plan_update(
            make_config(min_age),
            installed_version=installed,
            availability=available(),
            now=now,
            snapshot=SNAPSHOT,
        )

    def test_installs_newest_release_old_enough_with_tag(self) -> None:
        # 2026-09-12 minus 7 days is 2026-09-05T12:00Z: 2026.9.1 (09-03)
        # qualifies, 2026.9.2 (09-05T19:13Z) does not yet.
        plan = self.plan("2026.8.2")
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_version, "2026.9.1")
        self.assertTrue(plan.use_tag)
        self.assertIsNone(plan.error)
        self.assertIn("2026.9.1 (published 2026-09-03, 8 days old)", plan.reason)
        self.assertIn("2026.9.4 (published 2026-09-11, 1 day old) waits", plan.reason)
        self.assertEqual(plan.next_version, "2026.9.2")
        self.assertEqual(
            plan.next_eligible_at,
            SNAPSHOT.published["2026.9.2"] + timedelta(days=7),
        )
        self.assertIn("Next: 2026.9.2 becomes eligible 2026-09-12T19:13Z", plan.reason)

    def test_waits_when_installed_is_already_the_newest_old_enough(self) -> None:
        plan = self.plan("2026.9.1")
        self.assertFalse(plan.install)
        self.assertIsNone(plan.error)
        self.assertIn("2026.9.4 (published 2026-09-11, 1 day old) is too new", plan.reason)
        self.assertIn("Installed 2026.9.1 is already the newest release old enough", plan.reason)
        self.assertEqual(plan.next_version, "2026.9.2")

    def test_never_downgrades_when_installed_is_ahead(self) -> None:
        plan = self.plan("2026.9.4")
        self.assertFalse(plan.install)
        self.assertIsNone(plan.error)
        self.assertIn("Installed 2026.9.4 is newer than 2026.9.1", plan.reason)

    def test_walks_the_release_train_over_time(self) -> None:
        expected = [
            (datetime(2026, 9, 12, 12, tzinfo=timezone.utc), "2026.9.1"),
            (datetime(2026, 9, 13, 7, tzinfo=timezone.utc), "2026.9.2"),
            (datetime(2026, 9, 16, 7, tzinfo=timezone.utc), "2026.9.3"),
            (datetime(2026, 9, 19, 7, tzinfo=timezone.utc), "2026.9.4"),
        ]
        for now, target in expected:
            with self.subTest(now=now.isoformat()):
                plan = self.plan("2026.8.2", now=now)
                self.assertTrue(plan.install)
                self.assertEqual(plan.target_version, target)

    def test_backport_on_older_line_does_not_win(self) -> None:
        # 2026-09-17: 2026.6.35 (published 09-10) is old enough but sorts
        # far below 2026.9.3, which is also old enough. Version order wins,
        # not publish order.
        plan = self.plan("2026.8.2", now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
        self.assertEqual(plan.target_version, "2026.9.3")

    def test_prereleases_are_never_candidates(self) -> None:
        # 2026-09-05: the beta from 08-28 is a week old, but the newest
        # eligible release is 2026.7.30 because 2026.8.1 landed 08-31.
        plan = self.plan("2026.7.1", now=datetime(2026, 9, 5, 12, tzinfo=timezone.utc))
        self.assertEqual(plan.target_version, "2026.7.30")

    def test_versions_above_latest_tag_are_ignored(self) -> None:
        plan = self.plan("2026.8.2", now=datetime(2026, 9, 30, 12, tzinfo=timezone.utc))
        self.assertEqual(plan.target_version, "2026.9.4")

    def test_unknown_installed_version_blocks(self) -> None:
        plan = self.plan(None)
        self.assertFalse(plan.install)
        self.assertEqual(plan.error, "installed version unknown")

    def test_zero_days_restores_plain_update(self) -> None:
        plan = self.plan("2026.8.2", min_age=0)
        self.assertTrue(plan.install)
        self.assertFalse(plan.use_tag)
        self.assertEqual(plan.target_version, "2026.9.4")
        self.assertIn("MIN_UPDATE_AGE_DAYS=0", plan.reason)

    def test_registry_failure_blocks_with_error(self) -> None:
        with mock.patch(
            "mechanic.release_age.fetch_registry_snapshot",
            return_value=(None, "npm not found next to /fake/openclaw or on PATH"),
        ):
            plan = plan_update(
                make_config(),
                installed_version="2026.8.2",
                availability=available(),
                now=NOW,
            )
        self.assertFalse(plan.install)
        self.assertEqual(plan.error, "npm not found next to /fake/openclaw or on PATH")
        self.assertIn("2026.9.4 stays uninstalled", plan.reason)
        self.assertIn("MIN_UPDATE_AGE_DAYS=0", plan.reason)


class OtherChannelPlanTests(unittest.TestCase):
    def test_extended_stable_waits_then_updates_without_tag(self) -> None:
        avail = available(latest="2026.6.35", channel="extended-stable")
        waiting = plan_update(
            make_config(), installed_version="2026.6.30", availability=avail,
            now=NOW, snapshot=SNAPSHOT,
        )
        self.assertFalse(waiting.install)
        self.assertIsNone(waiting.error)
        self.assertIn("extended-stable channel is too new", waiting.reason)
        self.assertEqual(waiting.next_version, "2026.6.35")
        self.assertIn("becomes eligible 2026-09-17T04:51Z", waiting.reason)

        later = plan_update(
            make_config(), installed_version="2026.6.30", availability=avail,
            now=datetime(2026, 9, 18, 7, tzinfo=timezone.utc), snapshot=SNAPSHOT,
        )
        self.assertTrue(later.install)
        self.assertEqual(later.target_version, "2026.6.35")
        self.assertFalse(later.use_tag)

    def test_beta_channel_pins_with_tag_once_old_enough(self) -> None:
        avail = available(latest="2026.9.1-beta.1", channel="beta")
        plan = plan_update(
            make_config(), installed_version="2026.8.1", availability=avail,
            now=NOW, snapshot=SNAPSHOT,
        )
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_version, "2026.9.1-beta.1")
        self.assertTrue(plan.use_tag)

    def test_unknown_version_on_channel_blocks(self) -> None:
        avail = available(latest="2026.9.99", channel="beta")
        plan = plan_update(
            make_config(), installed_version="2026.8.1", availability=avail,
            now=NOW, snapshot=SNAPSHOT,
        )
        self.assertFalse(plan.install)
        self.assertEqual(plan.error, "version not on registry")


class ChannelExtractionTests(unittest.TestCase):
    def test_reads_plausible_shapes(self) -> None:
        self.assertEqual(_extract_channel({"channel": "Beta"}), "beta")
        self.assertEqual(_extract_channel({"availability": {"channel": "stable"}}), "stable")
        self.assertEqual(_extract_channel({"update": {"channel": {"name": "dev"}}}), "dev")

    def test_missing_or_odd_shapes_yield_none(self) -> None:
        self.assertIsNone(_extract_channel({}))
        self.assertIsNone(_extract_channel({"channel": 7}))
        self.assertIsNone(_extract_channel({"availability": {"available": True}}))
        self.assertIsNone(_extract_channel("stable"))


if __name__ == "__main__":
    unittest.main()
