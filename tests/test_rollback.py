"""Tests for snapshot capture and restore with per-target exclusions."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from mechanic.rollback import (
    KIND_LAST_KNOWN_GOOD,
    KIND_NIGHTLY,
    SnapshotStore,
    capture_snapshot,
    find_snapshot,
    get_sticky,
    latest_nightly,
    prune_nightlies,
    restore_snapshot,
)


class RollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mechanic-rollback-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "hermes-home"
        (self.source / "skills" / "a").mkdir(parents=True)
        (self.source / "hermes-agent").mkdir()
        (self.source / "installs").mkdir()
        (self.source / "skills" / "a" / "__pycache__").mkdir()
        (self.source / "config.yaml").write_text("v1\n")
        (self.source / "skills" / "a" / "SKILL.md").write_text("one\n")
        (self.source / "skills" / "a" / "__pycache__" / "x.pyc").write_text("pyc\n")
        (self.source / "hermes-agent" / "code.py").write_text("code\n")
        (self.source / "installs" / "python").write_text("runtime\n")
        self.store = SnapshotStore(
            name="hermes", root=self.tmp / "snapshots" / "hermes", source=self.source,
            excludes=("hermes-agent", "installs"), exclude_patterns=("*/__pycache__",),
        )

    def test_capture_excludes_and_restore_keeps_excluded_entries(self) -> None:
        snap = capture_snapshot(self.store, kind=KIND_NIGHTLY, version="v1", extra={"code_sha": "abc"})
        self.assertEqual(snap.version, "v1")
        self.assertEqual(snap.extra["code_sha"], "abc")
        listing = _tar_list(snap.archive_path)
        self.assertIn("hermes-home/config.yaml", listing)
        self.assertNotIn("hermes-home/hermes-agent/code.py", listing)
        self.assertNotIn("hermes-home/installs/python", listing)
        self.assertNotIn("hermes-home/skills/a/__pycache__/x.pyc", listing)

        # Mutate the live data and the excluded entries, then restore.
        (self.source / "config.yaml").write_text("v2\n")
        (self.source / "hermes-agent" / "code.py").write_text("newer code\n")
        (self.source / "installs" / "python").write_text("newer runtime\n")
        restore_snapshot(self.store, snap)

        self.assertEqual((self.source / "config.yaml").read_text(), "v1\n")
        self.assertEqual((self.source / "hermes-agent" / "code.py").read_text(), "newer code\n")
        self.assertEqual((self.source / "installs" / "python").read_text(), "newer runtime\n")
        self.assertFalse(any(self.source.parent.glob(".hermes-home.pre-restore-*")))

    def test_sticky_and_nightly_lookup_and_prune(self) -> None:
        first = capture_snapshot(self.store, kind=KIND_NIGHTLY, snapshot_id="2026-10-01T02-00-00Z")
        capture_snapshot(self.store, kind=KIND_NIGHTLY, snapshot_id="2026-10-02T02-00-00Z")
        newest = capture_snapshot(self.store, kind=KIND_NIGHTLY, snapshot_id="2026-10-03T02-00-00Z")
        lkg = capture_snapshot(self.store, kind=KIND_LAST_KNOWN_GOOD, version="v1")
        self.assertEqual(latest_nightly(self.store).snapshot_id, newest.snapshot_id)
        self.assertEqual(get_sticky(self.store, KIND_LAST_KNOWN_GOOD).path, lkg.path)
        self.assertEqual(find_snapshot(self.store, first.snapshot_id).path, first.path)
        self.assertIsNone(find_snapshot(self.store, "nope"))
        removed = prune_nightlies(self.store, keep=2)
        self.assertEqual([p.name for p in removed], [first.snapshot_id])
        self.assertTrue(lkg.path.exists())


class SqliteSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        import sqlite3

        self.tmp = Path(tempfile.mkdtemp(prefix="mechanic-sqlite-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "hermes-home"
        (self.source / "cron").mkdir(parents=True)
        (self.source / "config.yaml").write_text("v1\n")
        for rel in ("state.db", "cron/executions.db"):
            conn = sqlite3.connect(self.source / rel)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('before')")
            conn.commit()
            # Keep a writer open so a WAL and shm file exist, as on a live host.
            self.addCleanup(conn.close)
            setattr(self, rel.replace("/", "_").replace(".", "_"), conn)
        self.store = SnapshotStore(
            name="hermes", root=self.tmp / "snapshots" / "hermes", source=self.source,
            excludes=("state-snapshots",), sqlite_globs=("*.db", "cron/*.db"),
        )

    def test_databases_are_copied_safely_and_kept_out_of_the_tar(self) -> None:
        import sqlite3

        snap = capture_snapshot(self.store, kind=KIND_NIGHTLY, version="v1")
        listing = _tar_list(snap.archive_path)
        self.assertIn("hermes-home/config.yaml", listing)
        self.assertFalse(any(name.endswith((".db", ".db-wal", ".db-shm")) for name in listing), listing)
        self.assertTrue((snap.path / "sqlite" / "state.db.gz").is_file())
        self.assertTrue((snap.path / "sqlite" / "cron" / "executions.db.gz").is_file())

        # Change the live databases, then restore: rows come back, sidecars go.
        self.state_db.execute("UPDATE t SET v='after'")
        self.state_db.commit()
        self.state_db.close()
        self.cron_executions_db.close()
        restore_snapshot(self.store, snap)
        conn = sqlite3.connect(self.source / "state.db")
        self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "before")
        conn.close()
        self.assertFalse((self.source / "state.db-wal").exists())
        self.assertFalse((self.source / "state.db-shm").exists())
        conn = sqlite3.connect(self.source / "cron" / "executions.db")
        self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "before")
        conn.close()


def _tar_list(archive: Path) -> set[str]:
    import tarfile

    with tarfile.open(archive, "r:gz") as tar:
        return {member.name for member in tar.getmembers()}


if __name__ == "__main__":
    unittest.main()
