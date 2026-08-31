"""Snapshot and restore for OpenClaw config.

Mechanic snapshots OpenClaw's config directory before every nightly update so
a verify failure can be rolled back. Snapshots are tar.gz archives, not raw
directory copies: OpenClaw is a live daemon that writes continuously to its
config directory, and a copytree-based snapshot races against those writes
(see the v0.1 install postmortem in SESSIONS.md 2026-05-23).

Disk layout (CLAUDE.md section 5):

    ~/Library/Application Support/tekrescue-mechanic/snapshots/
    |-- first-known-good/    pristine pre-Mechanic anchor, never auto-pruned
    |   |-- metadata.json
    |   `-- archive.tar.gz
    |-- last-known-good/     latest verified-working state, never auto-pruned
    |   |-- metadata.json
    |   `-- archive.tar.gz
    `-- nightly/
        `-- <YYYY-MM-DDTHH-MM-SSZ>/   rolling, pruned by SNAPSHOT_RETENTION_DAYS
            |-- metadata.json
            `-- archive.tar.gz

`tmp/` and `logs/` inside OpenClaw's config dir are excluded from snapshots:
they're high-churn caches with no recovery value, and they're the source of
the live-daemon race that broke v0.1's first install attempt.

Restore is operator-driven in v0.1 (via `mechanic restore <target>`). It
stops the OpenClaw daemon, untars over the config dir, and restarts the
daemon. The nightly updater never auto-restores in v0.1; on verify failure
it logs and notifies, and the operator decides whether to roll back.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Config, SNAPSHOT_DIR, clean_subprocess_env


KIND_FIRST_KNOWN_GOOD = "first-known-good"
KIND_LAST_KNOWN_GOOD = "last-known-good"
KIND_NIGHTLY = "nightly"
VALID_KINDS = (KIND_FIRST_KNOWN_GOOD, KIND_LAST_KNOWN_GOOD, KIND_NIGHTLY)

NIGHTLY_DIR = SNAPSHOT_DIR / "nightly"
FIRST_KNOWN_GOOD_DIR = SNAPSHOT_DIR / KIND_FIRST_KNOWN_GOOD
LAST_KNOWN_GOOD_DIR = SNAPSHOT_DIR / KIND_LAST_KNOWN_GOOD

ARCHIVE_FILENAME = "archive.tar.gz"
METADATA_FILENAME = "metadata.json"

# Subdirectories of OPENCLAW_CONFIG_PATH that are excluded from snapshots.
# These are high-churn caches that race against the live daemon and have no
# recovery value.
DEFAULT_EXCLUDES = ("tmp", "logs")

_TAR_TIMEOUT_SECONDS = 600

_LOG = logging.getLogger(__name__)


class RollbackError(Exception):
    """Raised when a snapshot or restore operation fails."""


class InsufficientDiskError(RollbackError):
    """Raised when free disk is below MIN_FREE_DISK_MB_FOR_SNAPSHOT."""


@dataclass(frozen=True)
class Snapshot:
    """Pointer to a snapshot directory plus its parsed metadata."""

    path: Path
    snapshot_id: str
    kind: str
    captured_at: str
    openclaw_version: Optional[str]
    openclaw_config_source: str
    verified_healthy: bool

    @property
    def archive_path(self) -> Path:
        """Path to the tar.gz archive inside this snapshot."""
        return self.path / ARCHIVE_FILENAME


def free_disk_mb(path: Path) -> int:
    """Return the free space (in megabytes) on the filesystem holding `path`.

    If the path does not exist yet, walks up to its first existing parent so
    install-time callers can probe before SNAPSHOT_DIR is created.
    """
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return usage.free // (1024 * 1024)


# Emergency pruning never removes the newest N nightlies, no matter how
# short on disk we are. Recent restore points outrank a completed run.
EMERGENCY_PRUNE_MIN_KEEP = 3


def _archive_mb_from_metadata(snapshot_dir: Path) -> int:
    """Read archive_size_bytes from a snapshot's metadata.json, in MB.

    Returns 0 when the metadata is missing or unreadable so callers can
    fall back to the configured floor.
    """
    meta_path = snapshot_dir / METADATA_FILENAME
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return int(meta.get("archive_size_bytes", 0)) // (1024 * 1024)
    except (OSError, ValueError, TypeError):
        return 0


def _estimate_next_archive_mb() -> int:
    """Best guess at the next archive's size, in MB.

    Prefers last-known-good's recorded size (refreshed after every
    successful run), falls back to the newest nightly, else 0.
    """
    if LAST_KNOWN_GOOD_DIR.is_dir():
        mb = _archive_mb_from_metadata(LAST_KNOWN_GOOD_DIR)
        if mb:
            return mb
    newest = latest_nightly()
    if newest is not None:
        return _archive_mb_from_metadata(newest.path)
    return 0


def ensure_disk_headroom(config: Config) -> list[str]:
    """Pre-flight disk check with emergency pruning of the oldest nightlies.

    A run's peak transient need is roughly TWO archives: the nightly
    snapshot itself, plus the last-known-good rewrite after a successful
    verify (which tars into a tmp sibling before the old copy is removed).
    We require that much free space (with a 10 percent growth margin, and
    never less than MIN_FREE_DISK_MB_FOR_SNAPSHOT), and if the volume is
    short we delete the OLDEST nightly snapshots one at a time until the
    run fits. Sticky snapshots (first-known-good, last-known-good) are
    never touched, and the newest EMERGENCY_PRUNE_MIN_KEEP nightlies
    always survive.

    Returns the list of pruned snapshot ids, empty when nothing was
    pruned. Raises InsufficientDiskError if space is still short after
    pruning. Per CLAUDE.md section 5: we never proceed with an update if
    a rollback would not be possible.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    est_mb = _estimate_next_archive_mb()
    required = max(config.min_free_disk_mb_for_snapshot, (est_mb * 2 * 11) // 10)
    pruned: list[str] = []
    while free_disk_mb(SNAPSHOT_DIR) < required:
        if not NIGHTLY_DIR.exists():
            break
        victims = sorted(
            (
                p
                for p in NIGHTLY_DIR.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ),
            key=lambda p: p.name,
        )
        if len(victims) <= EMERGENCY_PRUNE_MIN_KEEP:
            break
        victim = victims[0]
        try:
            shutil.rmtree(victim)
        except OSError as exc:
            _LOG.warning("emergency prune could not remove %s: %s", victim, exc)
            break
        pruned.append(victim.name)
        _LOG.warning(
            "emergency-pruned oldest nightly snapshot %s to free disk "
            "(free now %d MB, run needs %d MB)",
            victim.name,
            free_disk_mb(SNAPSHOT_DIR),
            required,
        )
    free_mb = free_disk_mb(SNAPSHOT_DIR)
    if free_mb < required:
        after = (
            f" even after emergency-pruning {len(pruned)} old snapshot(s)"
            if pruned
            else ""
        )
        raise InsufficientDiskError(
            f"Free disk on snapshot volume is {free_mb} MB but this run needs "
            f"about {required} MB{after}. "
            f"Refusing to start an update without rollback headroom."
        )
    return pruned


def capture_snapshot(
    config: Config,
    *,
    kind: str,
    openclaw_version: Optional[str] = None,
    verified_healthy: bool = False,
    snapshot_id: Optional[str] = None,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
) -> Snapshot:
    """Snapshot OpenClaw's config directory as a tar.gz archive.

    Atomic on the same filesystem: tar writes into a hidden tmp sibling and
    the directory is renamed into place after metadata.json is written. For
    sticky kinds (first-known-good, last-known-good) this replaces whatever
    was there.

    Args:
        config: Loaded Mechanic configuration.
        kind: One of VALID_KINDS.
        openclaw_version: OpenClaw's reported version at capture time.
        verified_healthy: True if OpenClaw passed a health check at capture.
        snapshot_id: Override the auto-generated id. Only used by tests.
        excludes: Subdirectory names (relative to OPENCLAW_CONFIG_PATH) to
            skip during archive creation. Defaults to ('tmp', 'logs').

    Raises:
        RollbackError: kind invalid, source missing, or tar invocation fails.
    """
    if kind not in VALID_KINDS:
        raise RollbackError(
            f"Unknown snapshot kind {kind!r}. Must be one of {VALID_KINDS}."
        )

    source = config.openclaw_config_path
    if not source.exists():
        raise RollbackError(
            f"OpenClaw config path {source} does not exist; nothing to snapshot."
        )
    if not source.is_dir():
        raise RollbackError(
            f"OpenClaw config path {source} is not a directory."
        )

    sid = snapshot_id or _generate_snapshot_id()
    destination = _destination_for(kind, sid)
    destination.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = destination.with_name(f".{destination.name}.tmp-{sid}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    archive_tmp = tmp_dir / ARCHIVE_FILENAME

    try:
        _run_tar_create(source, archive_tmp, excludes=excludes)
        metadata = {
            "snapshot_id": sid,
            "kind": kind,
            "captured_at": _now_iso(),
            "openclaw_version": openclaw_version,
            "openclaw_config_source": str(source),
            "verified_healthy": bool(verified_healthy),
            "archive_filename": ARCHIVE_FILENAME,
            "excludes": list(excludes),
            "archive_size_bytes": archive_tmp.stat().st_size,
        }
        (tmp_dir / METADATA_FILENAME).write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _swap_into_place(tmp_dir, destination)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    _LOG.info(
        "captured %s snapshot at %s (%d MB)",
        kind,
        destination,
        metadata["archive_size_bytes"] // (1024 * 1024),
    )
    return _snapshot_from_path(destination)


def restore_snapshot(config: Config, snapshot: Snapshot) -> None:
    """Replace OpenClaw's config directory with the contents of `snapshot`.

    CALLER MUST STOP THE OPENCLAW DAEMON FIRST. This function does not stop
    or start the daemon; the `mechanic restore` CLI wraps the lifecycle.
    If the daemon is running while we restore, it will recreate files in
    the config dir mid-untar and the restore will be incomplete.

    Strategy: move the live config dir to a sibling `.pre-restore-*`, untar
    the archive into the original location, then remove the backup on
    success (or roll the rename back on failure).
    """
    if not snapshot.archive_path.is_file():
        raise RollbackError(
            f"Snapshot at {snapshot.path} is missing {ARCHIVE_FILENAME}."
        )

    target = config.openclaw_config_path
    target.parent.mkdir(parents=True, exist_ok=True)

    backup: Optional[Path] = None
    if target.exists():
        backup = target.with_name(
            f".{target.name}.pre-restore-{_generate_snapshot_id()}"
        )
        target.rename(backup)

    try:
        target.mkdir(parents=True, exist_ok=False)
        _run_tar_extract(snapshot.archive_path, into=target.parent, strip_to=target.name)
    except Exception:
        if backup is not None and not _has_meaningful_content(target):
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            backup.rename(target)
        raise

    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)

    _LOG.info("restored OpenClaw config from %s", snapshot.path)


def prune_nightlies(config: Config) -> list[Path]:
    """Trim nightly/ to at most SNAPSHOT_RETENTION_DAYS entries.

    Never touches first-known-good or last-known-good. Returns the list of
    paths that were removed (oldest first).
    """
    if not NIGHTLY_DIR.exists():
        return []

    nightlies = sorted(
        (p for p in NIGHTLY_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )
    keep = config.snapshot_retention_days
    if len(nightlies) <= keep:
        return []

    to_remove = nightlies[: len(nightlies) - keep]
    removed: list[Path] = []
    for path in to_remove:
        try:
            shutil.rmtree(path)
            removed.append(path)
            _LOG.info("pruned nightly snapshot %s", path.name)
        except OSError as exc:
            _LOG.warning("could not prune %s: %s", path, exc)
    return removed


def latest_nightly() -> Optional[Snapshot]:
    """Return the newest nightly snapshot, or None if there are none."""
    if not NIGHTLY_DIR.exists():
        return None
    candidates = sorted(
        (p for p in NIGHTLY_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )
    if not candidates:
        return None
    return _snapshot_from_path(candidates[-1])


def get_sticky(kind: str) -> Optional[Snapshot]:
    """Return the first- or last-known-good snapshot, or None if missing."""
    if kind == KIND_FIRST_KNOWN_GOOD:
        path = FIRST_KNOWN_GOOD_DIR
    elif kind == KIND_LAST_KNOWN_GOOD:
        path = LAST_KNOWN_GOOD_DIR
    else:
        raise RollbackError(
            f"get_sticky kind must be {KIND_FIRST_KNOWN_GOOD} or "
            f"{KIND_LAST_KNOWN_GOOD}. Got {kind!r}."
        )
    if not path.is_dir():
        return None
    return _snapshot_from_path(path)


def _run_tar_create(
    source: Path,
    archive: Path,
    *,
    excludes: tuple[str, ...],
) -> None:
    """Create `archive` as a gzipped tar of `source`'s contents.

    The archive stores `source` under its top-level basename so extraction
    recreates the original directory structure.
    """
    parent = source.parent
    basename = source.name
    cmd = ["tar", "-czf", str(archive)]
    for ex in excludes:
        cmd.extend(["--exclude", f"{basename}/{ex}"])
    cmd.extend(["-C", str(parent), basename])

    _LOG.info("snapshot: %s", " ".join(cmd))
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean_subprocess_env(),
            timeout=_TAR_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RollbackError(
            f"tar timed out after {_TAR_TIMEOUT_SECONDS}s while archiving {source}"
        ) from exc
    except OSError as exc:
        raise RollbackError(f"could not invoke tar: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        raise RollbackError(
            f"tar failed (exit {proc.returncode}, after {elapsed_ms} ms): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )
    _LOG.info(
        "snapshot: tar wrote %s in %d ms (%d bytes)",
        archive.name,
        elapsed_ms,
        archive.stat().st_size,
    )


def _run_tar_extract(archive: Path, *, into: Path, strip_to: str) -> None:
    """Extract `archive` into directory `into`.

    The archive is expected to contain a single top-level directory; the
    caller has already created the destination so extraction overlays its
    contents into the prepared `into / strip_to`. We use tar -C and trust
    the embedded paths because we wrote them ourselves.
    """
    cmd = ["tar", "-xzf", str(archive), "-C", str(into)]
    _LOG.info("restore: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean_subprocess_env(),
            timeout=_TAR_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RollbackError(
            f"tar timed out after {_TAR_TIMEOUT_SECONDS}s while extracting {archive}"
        ) from exc
    except OSError as exc:
        raise RollbackError(f"could not invoke tar: {exc}") from exc

    if proc.returncode != 0:
        raise RollbackError(
            f"tar extract failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )

    final = into / strip_to
    if not final.is_dir():
        raise RollbackError(
            f"tar extract completed but {final} does not exist. "
            f"Archive may have been written with the wrong top-level name."
        )


def _has_meaningful_content(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return any(path.iterdir())
    except OSError:
        return False


def _destination_for(kind: str, snapshot_id: str) -> Path:
    if kind == KIND_NIGHTLY:
        return NIGHTLY_DIR / snapshot_id
    if kind == KIND_FIRST_KNOWN_GOOD:
        return FIRST_KNOWN_GOOD_DIR
    return LAST_KNOWN_GOOD_DIR


def _swap_into_place(tmp_dir: Path, destination: Path) -> None:
    """Move `tmp_dir` to `destination`, replacing any existing directory."""
    if destination.exists():
        retired = destination.with_name(
            f".{destination.name}.replaced-{_generate_snapshot_id()}"
        )
        destination.rename(retired)
        try:
            tmp_dir.rename(destination)
        except Exception:
            retired.rename(destination)
            raise
        shutil.rmtree(retired, ignore_errors=True)
    else:
        tmp_dir.rename(destination)


def _snapshot_from_path(path: Path) -> Snapshot:
    meta_path = path / METADATA_FILENAME
    if not meta_path.exists():
        raise RollbackError(f"Snapshot at {path} is missing {METADATA_FILENAME}.")
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RollbackError(
            f"Could not read snapshot metadata at {meta_path}: {exc}"
        ) from exc
    return Snapshot(
        path=path,
        snapshot_id=data.get("snapshot_id", path.name),
        kind=data.get("kind", "unknown"),
        captured_at=data.get("captured_at", ""),
        openclaw_version=data.get("openclaw_version"),
        openclaw_config_source=data.get("openclaw_config_source", ""),
        verified_healthy=bool(data.get("verified_healthy", False)),
    )


def _generate_snapshot_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
