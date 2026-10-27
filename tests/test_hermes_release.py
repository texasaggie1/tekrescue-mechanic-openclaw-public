"""Tests for the Hermes release pinning logic (mechanic/hermes_release.py).

Run with: python3 -m unittest discover -s tests -v

These use real git: a bare `origin` with a linear history and release tags,
and a clone standing in for ~/.hermes/hermes-agent. The `hermes` binary is a
shell script that records its arguments, so the pin and rollback paths run
end to end without Hermes installed.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mechanic.config import HermesSettings
from mechanic import hermes_release
from mechanic.hermes_release import (
    MODE_HERMES_UPDATE,
    MODE_RELEASE_TAG,
    RELEASE_TAG_RE,
    TagLedger,
    TagRecord,
    apply_release_tag,
    describe_head,
    fetch_release_tags,
    head_commit,
    is_ancestor,
    list_upstream_tags,
    load_ledger,
    local_tag_dates,
    observe_tags,
    plan_update,
    save_ledger,
    scan_and_plan,
)


NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(*args: str, cwd: Path, date: str | None = None) -> str:
    env = dict(GIT_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()


class Repo:
    """A bare origin plus a working clone, with helpers to grow history."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.work = root / "work"       # where the maintainers commit
        self.source = root / "hermes-agent"  # Mechanic's view: the install checkout
        git("init", "--bare", "-q", str(self.origin), cwd=root)
        git("clone", "-q", str(self.origin), str(self.work), cwd=root)
        git("checkout", "-q", "-b", "main", cwd=self.work)
        self.commits: dict[str, str] = {}

    def commit(self, label: str, date: str) -> str:
        (self.work / f"{label}.txt").write_text(label + "\n")
        git("add", ".", cwd=self.work)
        git("commit", "-q", "-m", label, cwd=self.work, date=date)
        sha = git("rev-parse", "HEAD", cwd=self.work)
        self.commits[label] = sha
        return sha

    def tag(self, name: str, date: str, annotated: bool = True) -> None:
        if annotated:
            git("tag", "-a", "-m", name, name, cwd=self.work, date=date)
        else:
            git("tag", name, cwd=self.work)

    def push(self) -> None:
        git("push", "-q", "origin", "main", "--tags", cwd=self.work)

    def clone_install(self) -> None:
        git("clone", "-q", str(self.origin), str(self.source), cwd=self.root)
        git("checkout", "-q", "main", cwd=self.source)

    def move_tag(self, name: str, to_label: str) -> None:
        git("tag", "-f", "-a", "-m", name, name, self.commits[to_label], cwd=self.work)
        git("push", "-q", "-f", "origin", f"refs/tags/{name}", cwd=self.work)


def make_settings(repo: Repo, bin_path: Path, *, min_age: int = 7, mode: str = MODE_RELEASE_TAG,
                  skip: tuple[str, ...] = (), release_check: str = "none",
                  critical_min_age: int = 1,
                  critical_keywords: tuple[str, ...] = ("security", "critical", "cve-")) -> HermesSettings:
    # release_check defaults to none here: the fixture's origin is a local
    # path, so GitHub cannot vouch for its tags.
    return HermesSettings(
        bin_path=bin_path,
        home=repo.root / "home",
        source_dir=repo.source,
        update_mode=mode,
        update_channel=None,
        min_update_age_days=min_age,
        skip_tags=skip,
        doctor_fix=True,
        gateway_restart_timeout_seconds=60,
        release_check=release_check,
        critical_min_update_age_days=critical_min_age,
        critical_keywords=critical_keywords,
    )


def write_fake_hermes(path: Path, *, pm_install_exit: int = 0) -> Path:
    log = path.parent / "hermes-calls.log"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"hermes $*\" >> '{log}'\n"
        "case \"$1 $2\" in\n"
        f"  'pm install') exit {pm_install_exit} ;;\n"
        "esac\n"
        "exit 0\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return log


class HermesRepoTestCase(unittest.TestCase):
    """Builds this history on origin, dates in September 2026:

        c0 (09-01)  tag v0.22.1, plus v2026.9.1 (the retired dated shape,
                    numerically the largest tag of all: must be ignored)
        c1 (09-07)  tag v0.22.2
        c2 (09-14)  tag v0.22.3
        c3 (09-21)  tag v0.22.4 and v0.22.4-rc.1 (ignored shape)
        c4 (09-28)  v0.21.4+canary.20260928T070000Z (ignored shape)
        c5 (10-03)  tag v0.22.5
        c6 (10-04)  untagged tip of main
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mechanic-hermes-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = Repo(self.tmp)
        r = self.repo
        r.commit("c0", "2026-09-01T10:00:00Z"); r.tag("v0.22.1", "2026-09-01T10:05:00Z")
        r.tag("v2026.9.1", "2026-09-01T10:06:00Z")  # retired dated scheme
        r.commit("c1", "2026-09-07T10:00:00Z"); r.tag("v0.22.2", "2026-09-07T10:05:00Z")
        r.commit("c2", "2026-09-14T10:00:00Z"); r.tag("v0.22.3", "2026-09-14T10:05:00Z")
        r.commit("c3", "2026-09-21T10:00:00Z"); r.tag("v0.22.4", "2026-09-21T10:05:00Z")
        r.tag("v0.22.4-rc.1", "2026-09-20T10:05:00Z")
        r.commit("c4", "2026-09-28T10:00:00Z"); r.tag("v0.21.4+canary.20260928T070000Z", "2026-09-28T10:05:00Z", annotated=False)
        r.commit("c5", "2026-10-03T10:00:00Z"); r.tag("v0.22.5", "2026-10-03T10:05:00Z")
        r.commit("c6", "2026-10-04T10:00:00Z")
        r.push()
        r.clone_install()
        self.bin = self.tmp / "bin" / "hermes"
        self.bin.parent.mkdir()
        self.calls = write_fake_hermes(self.bin)
        self.settings = make_settings(self.repo, self.bin)
        self.ledger_path = self.tmp / "hermes_tags.json"

    def checkout(self, label_or_tag: str) -> None:
        git("checkout", "-q", "--detach", label_or_tag, cwd=self.repo.source)


class GitPlumbingTests(HermesRepoTestCase):
    def test_upstream_tags_are_peeled_to_commits(self) -> None:
        tags, error = list_upstream_tags(self.settings)
        self.assertIsNone(error)
        self.assertEqual(tags["v0.22.2"], self.repo.commits["c1"])
        self.assertEqual(tags["v0.22.4"], self.repo.commits["c3"])
        self.assertIn("v0.21.4+canary.20260928T070000Z", tags)

    def test_fetch_and_dates(self) -> None:
        self.assertIsNone(fetch_release_tags(self.settings))
        dates = local_tag_dates(self.settings)
        self.assertEqual(dates["v0.22.2"].date().isoformat(), "2026-09-07")
        self.assertIn("v0.21.4+canary.20260928T070000Z", dates)  # mirrored; the shape filter runs later
        self.assertNotIn("v0.22.4", git("tag", "-l", "mechanic*", cwd=self.repo.source))

    def test_release_tag_shape(self) -> None:
        # Hermes's own stable shape: the retired dated tags are not releases.
        self.assertTrue(RELEASE_TAG_RE.match("v0.22.4"))
        self.assertTrue(RELEASE_TAG_RE.match("v1.0.0"))
        self.assertTrue(RELEASE_TAG_RE.match("v999.0.10"))
        self.assertFalse(RELEASE_TAG_RE.match("v2026.9.24"))
        self.assertFalse(RELEASE_TAG_RE.match("v0.22.4-rc.1"))
        self.assertFalse(RELEASE_TAG_RE.match("v0.21.4+canary.20260928T070000Z"))
        self.assertFalse(RELEASE_TAG_RE.match("v01.2.3"))

    def test_describe_and_ancestry(self) -> None:
        self.checkout("v0.22.3")
        self.assertEqual(describe_head(self.settings), "v0.22.3")
        self.assertEqual(head_commit(self.settings), self.repo.commits["c2"])
        self.assertTrue(is_ancestor(self.settings, self.repo.commits["c2"], self.repo.commits["c3"]))
        self.assertFalse(is_ancestor(self.settings, self.repo.commits["c3"], self.repo.commits["c2"]))
        self.checkout("main")
        self.assertTrue(describe_head(self.settings).startswith("v0.22.5+1.g"))
        # The canary and rc tags exist on this history but must never be the label.
        git("tag", "v0.99.0+canary.20261004T000000Z", "main", cwd=self.repo.source)
        self.assertTrue(describe_head(self.settings).startswith("v0.22.5+1.g"))


class LedgerTests(HermesRepoTestCase):
    def test_bootstrap_trusts_tag_dates_once_then_first_sight(self) -> None:
        upstream, _ = list_upstream_tags(self.settings)
        fetch_release_tags(self.settings)
        dates = local_tag_dates(self.settings)
        ledger = observe_tags(TagLedger(), upstream, dates, now=NOW)
        self.assertTrue(ledger.bootstrapped)
        self.assertEqual(ledger.tags["v0.22.2"].first_seen.date().isoformat(), "2026-09-07")
        self.assertNotIn("v0.22.4-rc.1", ledger.tags)

        # A tag that appears later is dated by first sight, whatever it claims.
        self.repo.commit("c7", "2026-10-05T10:00:00Z")
        self.repo.tag("v0.22.6", "2026-09-01T00:00:00Z")  # backdated on purpose
        self.repo.push()
        later = NOW + timedelta(days=1)
        upstream, _ = list_upstream_tags(self.settings)
        fetch_release_tags(self.settings)
        ledger = observe_tags(ledger, upstream, local_tag_dates(self.settings), now=later)
        self.assertEqual(ledger.tags["v0.22.6"].first_seen, later)

    def test_moved_tag_is_flagged_and_kept(self) -> None:
        upstream, _ = list_upstream_tags(self.settings)
        fetch_release_tags(self.settings)
        ledger = observe_tags(TagLedger(), upstream, local_tag_dates(self.settings), now=NOW)
        original = ledger.tags["v0.22.3"].commit
        self.repo.move_tag("v0.22.3", "c5")
        upstream, _ = list_upstream_tags(self.settings)
        self.assertIsNone(fetch_release_tags(self.settings))  # mirrored into Mechanic's namespace
        ledger = observe_tags(ledger, upstream, local_tag_dates(self.settings), now=NOW)
        record = ledger.tags["v0.22.3"]
        self.assertTrue(record.moved)
        self.assertEqual(record.commit, original)
        self.assertEqual(record.moved_to, self.repo.commits["c5"])

    def test_round_trip(self) -> None:
        ledger = TagLedger(bootstrapped_at=NOW)
        ledger.tags["v1.2.3"] = TagRecord("v1.2.3", "a" * 40, NOW, created_at=NOW, moved=True, moved_to="b" * 40)
        save_ledger(ledger, self.ledger_path)
        loaded = load_ledger(self.ledger_path)
        self.assertEqual(loaded.bootstrapped_at, NOW)
        self.assertEqual(loaded.tags["v1.2.3"].moved_to, "b" * 40)
        self.assertTrue(loaded.tags["v1.2.3"].moved)


class PlanTests(HermesRepoTestCase):
    def ledger(self) -> tuple[TagLedger, dict[str, str]]:
        upstream, _ = list_upstream_tags(self.settings)
        fetch_release_tags(self.settings)
        return observe_tags(TagLedger(), upstream, local_tag_dates(self.settings), now=NOW), upstream

    def plan(self, head_label: str, published_fn=None, **kw):
        ledger, upstream = self.ledger()
        settings = make_settings(self.repo, self.bin, **kw)
        head = self.repo.commits[head_label]
        return plan_update(
            settings, ledger=ledger, upstream=upstream, head=head, installed_label=head_label,
            now=NOW, ancestor_fn=lambda a, b: is_ancestor(self.settings, a, b),
            published_fn=published_fn,
        )

    def test_dated_tags_are_not_releases(self) -> None:
        # v2026.9.1 outranks every semver tag numerically; v0.2.0 would have
        # chosen it and then reported the checkout "already ahead" forever.
        ledger, upstream = self.ledger()
        self.assertIn("v2026.9.1", upstream)
        self.assertNotIn("v2026.9.1", ledger.tags)
        plan = self.plan("c1")
        self.assertEqual(plan.target_tag, "v0.22.4")

    def test_unpublished_release_is_skipped_for_the_next_one(self) -> None:
        asked: list[str] = []

        def check(tag: str):
            asked.append(tag)
            return (False, f"{tag} has no GitHub Release (a tag, not a release)") if tag == "v0.22.4" else (True, "ok")

        plan = self.plan("c1", published_fn=check, critical_keywords=())
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_tag, "v0.22.3")
        self.assertEqual(asked, ["v0.22.4", "v0.22.3"])
        self.assertIn("v0.22.4 skipped: v0.22.4 has no GitHub Release (a tag, not a release)", plan.notes)

    def test_release_check_failure_installs_nothing(self) -> None:
        plan = self.plan("c1", published_fn=lambda tag: (None, "GitHub API refused the request (HTTP 403)"))
        self.assertFalse(plan.install)
        self.assertEqual(plan.error, "release check failed")
        self.assertIn("could not confirm that v0.22.4 is a published release", plan.reason)
        self.assertIn("installing nothing tonight", plan.reason)
        self.assertTrue(any(note.startswith("release check: GitHub API refused") for note in plan.notes))

    def test_unpublished_newer_tags_fall_back_to_the_tag_head_is_on(self) -> None:
        # HEAD sits on v0.22.2; v0.22.4 and v0.22.3 are tags without a
        # release. Mechanic reports "current" and names both skips.
        plan = self.plan("c1", published_fn=lambda tag: (False, f"{tag} is a prerelease"))
        self.assertFalse(plan.install)
        self.assertIsNone(plan.error)
        self.assertIn("already v0.22.2", plan.reason)
        self.assertEqual(len([n for n in plan.notes if "skipped" in n]), 2)

    def test_every_eligible_tag_unpublished_means_wait(self) -> None:
        # Only newer tags exist for this HEAD, none of them released yet.
        ledger, upstream = self.ledger()
        for old in ("v0.22.1", "v0.22.2"):
            del ledger.tags[old]
            del upstream[old]
        plan = plan_update(
            self.settings, ledger=ledger, upstream=upstream, head=self.repo.commits["c1"],
            installed_label="c1", now=NOW, ancestor_fn=lambda a, b: is_ancestor(self.settings, a, b),
            published_fn=lambda tag: (False, f"{tag} is a prerelease"),
        )
        self.assertFalse(plan.install)
        self.assertIsNone(plan.error)
        self.assertIn("no release old enough is a published GitHub Release yet", plan.reason)
        self.assertEqual(plan.next_tag, "v0.22.5")
        self.assertEqual(len([n for n in plan.notes if "skipped" in n]), 2)

    def test_critical_release_takes_the_fast_lane(self) -> None:
        # v0.22.5 was first seen 10-03, one day before NOW: pending under the
        # 7-day rule, eligible under the 1-day critical rule once its
        # release notes say so.
        def check(tag: str):
            critical = tag == "v0.22.5"
            return hermes_release.ReleaseVerdict(True, "ok", critical, "release notes mention 'security'" if critical else "")

        plan = self.plan("c1", published_fn=check)
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_tag, "v0.22.5")
        self.assertIn("marked critical, fast lane", plan.reason)
        self.assertTrue(any(note.startswith("fast lane: v0.22.5 is marked critical") for note in plan.notes))

    def test_pending_release_that_is_not_critical_still_waits(self) -> None:
        plan = self.plan("c1", published_fn=lambda tag: hermes_release.ReleaseVerdict(True, "ok"))
        self.assertEqual(plan.target_tag, "v0.22.4")
        self.assertNotIn("fast lane", plan.reason)

    def test_fast_lane_needs_the_release_check_and_keywords(self) -> None:
        self.assertEqual(self.plan("c1", published_fn=None).target_tag, "v0.22.4")
        critical = lambda tag: hermes_release.ReleaseVerdict(True, "ok", True, "release notes mention 'security'")
        self.assertEqual(self.plan("c1", published_fn=critical, critical_keywords=()).target_tag, "v0.22.4")
        self.assertEqual(self.plan("c1", published_fn=critical, critical_min_age=7).target_tag, "v0.22.4")

    def test_unknown_verdict_on_a_pending_tag_does_not_block_the_night(self) -> None:
        def check(tag: str):
            return hermes_release.ReleaseVerdict(None, "HTTP 403") if tag == "v0.22.5" else hermes_release.ReleaseVerdict(True, "ok")

        plan = self.plan("c1", published_fn=check)
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_tag, "v0.22.4")
        self.assertIn("release check for v0.22.5: HTTP 403; it waits the full period", plan.notes)

    def test_release_check_is_not_asked_when_nothing_would_be_pinned(self) -> None:
        asked: list[str] = []

        def check(tag: str):
            asked.append(tag)
            return True, "ok"

        self.assertFalse(self.plan("c3", published_fn=check, critical_keywords=()).install)  # current
        self.assertFalse(self.plan("c6", published_fn=check, critical_keywords=()).install)  # ahead
        self.assertEqual(asked, [])
        # With the fast lane on, only the tag inside its window is asked.
        self.assertFalse(self.plan("c6", published_fn=check).install)
        self.assertEqual(asked, ["v0.22.5"])

    def test_pins_newest_tag_old_enough_when_head_is_behind(self) -> None:
        # 2026-10-04 minus 7 days is 09-27: v0.22.4 qualifies, v0.22.5 (10-03) does not.
        plan = self.plan("c1")
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_tag, "v0.22.4")
        self.assertEqual(plan.target_commit, self.repo.commits["c3"])
        self.assertEqual(plan.next_tag, "v0.22.5")
        self.assertEqual(plan.next_eligible_at.date().isoformat(), "2026-10-10")
        self.assertIn("pinning the checkout", plan.reason)

    def test_current_when_head_is_the_target(self) -> None:
        plan = self.plan("c3")
        self.assertFalse(plan.install)
        self.assertIsNone(plan.error)
        self.assertIn("already v0.22.4", plan.reason)
        self.assertIn("Next: v0.22.5 becomes eligible 2026-10-10", plan.reason)

    def test_never_downgrades_when_head_is_ahead(self) -> None:
        plan = self.plan("c6")  # tip of main, past every tag
        self.assertFalse(plan.install)
        self.assertIsNone(plan.error)
        self.assertIn("already ahead of v0.22.4", plan.reason)
        self.assertIn("never downgrades", plan.reason)

    def test_skip_list_moves_to_the_next_release(self) -> None:
        plan = self.plan("c1", skip=("v0.22.4",))
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_tag, "v0.22.3")
        self.assertIn("v0.22.4 skipped (HERMES_SKIP_TAGS)", plan.notes)

    def test_moved_tag_is_never_installed(self) -> None:
        ledger, upstream = self.ledger()
        self.repo.move_tag("v0.22.4", "c6")
        upstream, _ = list_upstream_tags(self.settings)
        fetch_release_tags(self.settings)
        ledger = observe_tags(ledger, upstream, local_tag_dates(self.settings), now=NOW)
        plan = plan_update(
            self.settings, ledger=ledger, upstream=upstream, head=self.repo.commits["c1"],
            installed_label="c1", now=NOW, ancestor_fn=lambda a, b: is_ancestor(self.settings, a, b),
        )
        self.assertEqual(plan.target_tag, "v0.22.3")
        self.assertTrue(any("MOVED" in note for note in plan.notes))

    def test_zero_days_takes_the_newest_tag(self) -> None:
        plan = self.plan("c1", min_age=0)
        self.assertEqual(plan.target_tag, "v0.22.5")

    def test_hermes_update_mode_says_it_installs_the_tip(self) -> None:
        plan = self.plan("c1", mode=MODE_HERMES_UPDATE)
        self.assertTrue(plan.install)
        self.assertEqual(plan.mode, MODE_HERMES_UPDATE)
        self.assertIn("hermes update --yes", plan.reason)
        self.assertIn("cannot pin", plan.reason)

    def test_diverged_checkout_is_left_alone(self) -> None:
        # A commit on a side branch that no release tag descends from.
        git("checkout", "-q", "-b", "side", self.repo.commits["c1"], cwd=self.repo.source)
        (self.repo.source / "local.txt").write_text("x\n")
        git("add", ".", cwd=self.repo.source)
        git("commit", "-q", "-m", "side", cwd=self.repo.source, date="2026-10-01T00:00:00Z")
        side = head_commit(self.settings)
        ledger, upstream = self.ledger()
        plan = plan_update(
            self.settings, ledger=ledger, upstream=upstream, head=side,
            installed_label="side", now=NOW, ancestor_fn=lambda a, b: is_ancestor(self.settings, a, b),
        )
        self.assertFalse(plan.install)
        self.assertEqual(plan.error, "checkout diverged")


class PublicationCheckTests(unittest.TestCase):
    """GitHub answers, parsed the way Hermes's own updater parses them."""

    @staticmethod
    def answer(status: int, body=None, *, broken: bool = False):
        class Response:
            status_code = status

            def json(self):
                if broken:
                    raise ValueError("not json")
                return body

        return lambda url, headers, timeout: Response()

    def test_published_release_verdicts(self) -> None:
        ok = {"tag_name": "v0.22.4", "draft": False, "prerelease": False}
        self.assertEqual(hermes_release.published_release("o/r", "v0.22.4", get=self.answer(200, ok))[0], True)
        self.assertEqual(hermes_release.published_release("o/r", "v0.22.4", get=self.answer(200, {**ok, "prerelease": True}))[0], False)
        self.assertEqual(hermes_release.published_release("o/r", "v0.22.4", get=self.answer(200, {**ok, "draft": True}))[0], False)
        self.assertEqual(hermes_release.published_release("o/r", "v0.22.4", get=self.answer(404))[0], False)
        self.assertIsNone(hermes_release.published_release("o/r", "v0.22.4", get=self.answer(403))[0])
        self.assertIsNone(hermes_release.published_release("o/r", "v0.22.4", get=self.answer(500))[0])
        self.assertIsNone(hermes_release.published_release("o/r", "v0.22.4", get=self.answer(200, {**ok, "tag_name": "v0.22.5"}))[0])
        self.assertIsNone(hermes_release.published_release("o/r", "v0.22.4", get=self.answer(200, broken=True))[0])

        def boom(url, headers, timeout):
            raise OSError("no route to host")

        verdict = hermes_release.published_release("o/r", "v0.22.4", get=boom)
        self.assertIsNone(verdict.published)
        self.assertIn("could not be reached", verdict.why)

    def test_critical_keywords_in_name_or_notes(self) -> None:
        keywords = ("security", "critical", "cve-")
        base = {"tag_name": "v0.22.4", "draft": False, "prerelease": False}
        for release, expected in (
            ({**base, "name": "v0.22.4", "body": "Routine fixes."}, False),
            ({**base, "name": "v0.22.4", "body": "Fixes a SECURITY issue in the gateway."}, True),
            ({**base, "name": "Critical hotfix", "body": None}, True),
            ({**base, "name": "v0.22.4", "body": "Addresses CVE-2026-1234."}, True),
        ):
            verdict = hermes_release.published_release("o/r", "v0.22.4", keywords=keywords, get=self.answer(200, release))
            self.assertTrue(verdict.published)
            self.assertEqual(verdict.critical, expected, release)
        off = hermes_release.published_release(
            "o/r", "v0.22.4", keywords=(), get=self.answer(200, {**base, "name": "Critical security fix"}),
        )
        self.assertFalse(off.critical)

    def test_request_shape(self) -> None:
        seen = {}

        def get(url, headers, timeout):
            seen.update(url=url, headers=headers, timeout=timeout)
            return self.answer(404)(url, headers, timeout)

        hermes_release.published_release("NousResearch/hermes-agent", "v0.22.4", get=get)
        self.assertEqual(seen["url"], "https://api.github.com/repos/NousResearch/hermes-agent/releases/tags/v0.22.4")
        self.assertTrue(seen["headers"]["User-Agent"].startswith("tekrescue-mechanic/"))
        self.assertEqual(seen["headers"]["Accept"], "application/vnd.github+json")

    def test_repository_from_url(self) -> None:
        for url in (
            "https://github.com/NousResearch/hermes-agent.git",
            "https://github.com/NousResearch/hermes-agent",
            "git@github.com:NousResearch/hermes-agent.git",
            "ssh://git@github.com/NousResearch/hermes-agent",
        ):
            self.assertEqual(hermes_release.repository_from_url(url), "NousResearch/hermes-agent", url)
        self.assertIsNone(hermes_release.repository_from_url("https://gitlab.com/x/y.git"))
        self.assertIsNone(hermes_release.repository_from_url("/Users/me/origin.git"))


class ReleaseCheckerTests(HermesRepoTestCase):
    def test_disabled_means_no_checker(self) -> None:
        self.assertIsNone(hermes_release.release_checker(make_settings(self.repo, self.bin, release_check="none")))

    def test_non_github_origin_cannot_be_verified(self) -> None:
        check = hermes_release.release_checker(make_settings(self.repo, self.bin, release_check="github"))
        self.assertIsNotNone(check)
        verdict = check("v0.22.4")
        self.assertIsNone(verdict.published)
        self.assertIn("not a github.com repository", verdict.why)
        self.assertIn("HERMES_RELEASE_CHECK=none", verdict.why)
        # And the end-to-end plan then installs nothing, loudly.
        self.checkout("v0.22.2")
        plan = scan_and_plan(make_settings(self.repo, self.bin, release_check="github"), now=NOW, ledger_path=self.ledger_path)
        self.assertFalse(plan.install)
        self.assertEqual(plan.error, "release check failed")


class ScanAndApplyTests(HermesRepoTestCase):
    def test_scan_and_plan_end_to_end_writes_the_ledger(self) -> None:
        self.checkout("v0.22.2")
        plan = scan_and_plan(self.settings, now=NOW, ledger_path=self.ledger_path)
        self.assertTrue(plan.install)
        self.assertEqual(plan.target_tag, "v0.22.4")
        self.assertEqual(plan.installed_label, "v0.22.2")
        self.assertTrue(self.ledger_path.exists())
        self.assertTrue(load_ledger(self.ledger_path).bootstrapped)

    def test_missing_checkout_is_an_error_plan(self) -> None:
        settings = make_settings(self.repo, self.bin)
        shutil.rmtree(self.repo.source)
        plan = scan_and_plan(settings, now=NOW, ledger_path=self.ledger_path)
        self.assertFalse(plan.install)
        self.assertIn("does not exist", plan.error)

    def test_apply_pins_and_runs_pm_install(self) -> None:
        self.checkout("v0.22.2")
        plan = scan_and_plan(self.settings, now=NOW, ledger_path=self.ledger_path)
        result = apply_release_tag(self.settings, plan)
        self.assertTrue(result.success, result.summary)
        self.assertEqual(head_commit(self.settings), self.repo.commits["c3"])
        self.assertEqual(result.code_after, self.repo.commits["c3"])
        self.assertIn("hermes pm install", self.calls.read_text())
        self.assertEqual(describe_head(self.settings), "v0.22.4")

    def test_apply_refuses_a_dirty_checkout(self) -> None:
        self.checkout("v0.22.2")
        (self.repo.source / "c0.txt").write_text("edited\n")
        plan = scan_and_plan(self.settings, now=NOW, ledger_path=self.ledger_path)
        result = apply_release_tag(self.settings, plan)
        self.assertFalse(result.success)
        self.assertIn("local changes", result.summary)
        self.assertEqual(head_commit(self.settings), self.repo.commits["c1"])

    def test_apply_rolls_the_code_back_when_pm_install_fails(self) -> None:
        write_fake_hermes(self.bin, pm_install_exit=3)
        self.checkout("v0.22.2")
        plan = scan_and_plan(self.settings, now=NOW, ledger_path=self.ledger_path)
        result = apply_release_tag(self.settings, plan)
        self.assertFalse(result.success)
        self.assertTrue(result.rolled_back_code)
        self.assertEqual(head_commit(self.settings), self.repo.commits["c1"])
        self.assertEqual(result.code_after, self.repo.commits["c1"])
        self.assertIn("checkout put back on", result.summary)


class DoctorSummaryTests(unittest.TestCase):
    """The Doctor line of the report is one plain line, never doctor's findings."""

    DOCTOR_FIX_OUTPUT = (
        "\x1b[1mHermes Doctor\x1b[0m\n"
        "  \x1b[32m\u2713 Python 3.12\x1b[0m\n"
        "\x1b[32m\u2500\u2500\u2500\u2500\u2500\u2500\x1b[0m\n"
        "\x1b[32m\x1b[1m  Fixed 1 issue(s).\x1b[0m\x1b[33m\x1b[1m 5 issue(s) require manual intervention.\x1b[0m\n\n"
        "  1. gateway.pid stale\n"
        "  5. session_reset.mode: both is no longer applied: run `hermes plugins install x`.\n\n"
    )

    def test_fix_summary_counts_findings_without_repeating_them(self) -> None:
        result = hermes_release.CommandResult(ok=False, exit_code=1, stdout=self.DOCTOR_FIX_OUTPUT, stderr="")
        line, ok = hermes_release.summarize_doctor("hermes doctor --fix", result)
        self.assertFalse(ok)
        self.assertEqual(
            line,
            "hermes doctor --fix exit 1: fixed 1, 5 finding(s) need the operator "
            "(run `hermes doctor` to read them)",
        )
        self.assertNotIn("\n", line)
        self.assertNotIn("\x1b", line)
        self.assertNotIn("session_reset", line)

    def test_read_only_summary(self) -> None:
        out = "\x1b[33m  Found 3 issue(s) to address:\x1b[0m\n\n  1. a\n  2. b\n  3. c\n"
        result = hermes_release.CommandResult(ok=False, exit_code=1, stdout=out, stderr="")
        line, ok = hermes_release.summarize_doctor("hermes doctor", result)
        self.assertEqual(line, "hermes doctor exit 1: 3 finding(s) need the operator (run `hermes doctor` to read them)")

    def test_clean_run_and_unrecognised_output(self) -> None:
        result = hermes_release.CommandResult(ok=True, exit_code=0, stdout="All checks passed!", stderr="")
        self.assertEqual(hermes_release.summarize_doctor("hermes doctor", result), ("hermes doctor exit 0", True))
        result = hermes_release.CommandResult(ok=False, exit_code=2, stdout="", stderr="boom\r\nline two\x1b[0m")
        line, ok = hermes_release.summarize_doctor("hermes doctor", result)
        self.assertEqual(line, "hermes doctor exit 2: boom line two")

    def test_plain_text_strips_escapes_and_control_characters(self) -> None:
        raw = "\x1b[2K\rprogress 10%\x1b[1A\x1b]0;title\x07done\n\tnext"
        self.assertEqual(hermes_release.plain_text(raw), "progress 10%done next")
        tail = hermes_release.CommandResult(ok=False, exit_code=1, stdout="a\nb\n\nc", stderr="").tail()
        self.assertEqual(tail, "a b c")


if __name__ == "__main__":
    unittest.main()
