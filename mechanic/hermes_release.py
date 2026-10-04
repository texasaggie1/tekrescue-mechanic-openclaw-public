"""Hermes Agent release pinning (new in v0.2.0).

Hermes (github.com/NousResearch/hermes-agent) is a source install: a git
checkout at HERMES_SOURCE_DIR (default ~/.hermes/hermes-agent) that tracks
`main`, with user data in HERMES_HOME (~/.hermes). Its own updater,
`hermes update`, fast-forwards the checkout to the tip of `main` and has no
way to ask for an older release. The maintainers do mark releases: plain
tags such as v2026.9.24 land on `main` every few days, carry the date that
`hermes --version` prints, and run the project's release-gate tests.

So Mechanic pins. Each nightly (HERMES_UPDATE_MODE=release-tag):

  1. `git ls-remote --tags origin` is the upstream truth: tag names and the
     commits they point at, today. A fetch brings the tag objects and
     their history into the checkout under Mechanic's own ref namespace
     (refs/mechanic/upstream-tags/), so Hermes's refs/tags/ is never
     written by Mechanic and a moved upstream tag never collides with a
     local one. The working tree is untouched by the fetch.
  2. A ledger in Mechanic's state dir records when MECHANIC first saw each
     tag and which commit it pointed at. Age is counted from first sight,
     not from the tag's own date: git tag dates are written by whoever ran
     `git tag`, so a compromised account could backdate one straight past
     the waiting period. The one exception is the very first scan on a
     fresh install, which trusts the tags' recorded dates once so an
     operator does not wait a week for a release that is months old; that
     scan happens under supervision (`mechanic plan` shows the result).
  3. A tag whose commit differs from the one first recorded has MOVED.
     Tags are supposed to be immutable, so a moved tag is treated as
     hostile: never installed, reported loudly, cleared only by hand.
  4. The newest eligible tag (plain v-tag, not skipped, not moved, aged
     HERMES_MIN_UPDATE_AGE_DAYS) is compared with HEAD by git ancestry, not
     by version string: it is installed only if HEAD is one of its
     ancestors. HEAD already there means "current"; HEAD ahead (someone ran
     `hermes update`, or the checkout is on main's tip) means "wait for a
     newer tag"; anything else means "diverged, leaving the checkout alone".
  5. Install = `git checkout --detach <tag>`, then `hermes pm install`, the
     documented repair command that rebuilds the dependency environment
     for whatever the checkout is (Hermes's own `update` calls the same
     code), then `hermes doctor --fix` (safe config migrations, unattended)
     or plain `hermes doctor`, then a gateway restart through launchd if a
     gateway was running and the operator has not disabled it.

HERMES_UPDATE_MODE=hermes-update keeps Hermes's supported updater instead:
the same waiting period on the newest tag, then `hermes update --yes`,
which installs the tip of the configured channel (a trigger delay, not a
pin). Either mode leaves the install alone on any failure to establish the
facts, which is Mechanic's rule everywhere.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import HermesSettings, RUNTIME_STATE_DIR, clean_subprocess_env
from .release_age import version_key


_LOG = logging.getLogger(__name__)

RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

GIT_TIMEOUT_SECONDS = 180
PM_INSTALL_TIMEOUT_SECONDS = 3600
HERMES_UPDATE_TIMEOUT_SECONDS = 3600
DOCTOR_TIMEOUT_SECONDS = 900
BACKUP_TIMEOUT_SECONDS = 600

TAG_LEDGER_FILE = RUNTIME_STATE_DIR / "hermes_tags.json"
UPSTREAM_TAG_NAMESPACE = "refs/mechanic/upstream-tags/"

MODE_RELEASE_TAG = "release-tag"
MODE_HERMES_UPDATE = "hermes-update"


# ---------------------------------------------------------------------------
# Tag ledger (first sight, immutability)
# ---------------------------------------------------------------------------


@dataclass
class TagRecord:
    """What Mechanic knows about one upstream tag."""

    tag: str
    commit: str
    first_seen: datetime
    created_at: Optional[datetime] = None
    moved: bool = False
    moved_to: Optional[str] = None


@dataclass
class TagLedger:
    """Every release tag Mechanic has ever observed, persisted as JSON."""

    tags: dict[str, TagRecord] = field(default_factory=dict)
    bootstrapped_at: Optional[datetime] = None

    @property
    def bootstrapped(self) -> bool:
        return self.bootstrapped_at is not None


def load_ledger(path: Optional[Path] = None) -> TagLedger:
    """Read the ledger; a missing file is an empty, un-bootstrapped ledger."""
    target = path or TAG_LEDGER_FILE
    if not target.exists():
        return TagLedger()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("hermes: tag ledger %s unreadable (%s); starting empty", target, exc)
        return TagLedger()
    if not isinstance(data, dict):
        return TagLedger()
    ledger = TagLedger(bootstrapped_at=_parse_time(data.get("bootstrapped_at")))
    for tag, raw in (data.get("tags") or {}).items():
        if not isinstance(raw, dict):
            continue
        first_seen = _parse_time(raw.get("first_seen"))
        commit = raw.get("commit")
        if first_seen is None or not isinstance(commit, str):
            continue
        ledger.tags[tag] = TagRecord(
            tag=tag,
            commit=commit,
            first_seen=first_seen,
            created_at=_parse_time(raw.get("created_at")),
            moved=bool(raw.get("moved")),
            moved_to=raw.get("moved_to") if isinstance(raw.get("moved_to"), str) else None,
        )
    return ledger


def save_ledger(ledger: TagLedger, path: Optional[Path] = None) -> None:
    """Persist the ledger atomically (temp file, then rename)."""
    target = path or TAG_LEDGER_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bootstrapped_at": _fmt_iso(ledger.bootstrapped_at),
        "tags": {
            tag: {
                "commit": rec.commit,
                "first_seen": _fmt_iso(rec.first_seen),
                "created_at": _fmt_iso(rec.created_at),
                "moved": rec.moved,
                "moved_to": rec.moved_to,
            }
            for tag, rec in sorted(ledger.tags.items())
        },
    }
    fd, tmp = tempfile.mkstemp(prefix=".hermes_tags.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def observe_tags(
    ledger: TagLedger,
    upstream: dict[str, str],
    created: dict[str, datetime],
    *,
    now: datetime,
) -> TagLedger:
    """Fold today's upstream view into the ledger.

    New tags get `first_seen = now`. On the very first scan (an
    un-bootstrapped ledger) they get the earlier of now and their recorded
    creation date instead, the one-time trust described in the module
    docstring. A known tag now pointing elsewhere is marked moved and keeps
    its original commit so the plan can refuse it.
    """
    bootstrap = not ledger.bootstrapped
    for tag, commit in upstream.items():
        if not RELEASE_TAG_RE.match(tag):
            continue
        record = ledger.tags.get(tag)
        if record is None:
            first_seen = now
            created_at = created.get(tag)
            if bootstrap and created_at is not None and created_at < now:
                first_seen = created_at
            ledger.tags[tag] = TagRecord(
                tag=tag, commit=commit, first_seen=first_seen, created_at=created_at,
            )
            continue
        if record.created_at is None and tag in created:
            record.created_at = created[tag]
        if commit != record.commit:
            if not record.moved:
                _LOG.error(
                    "hermes: tag %s MOVED from %s to %s on origin; refusing it",
                    tag, record.commit, commit,
                )
            record.moved = True
            record.moved_to = commit
    if bootstrap:
        ledger.bootstrapped_at = now
    return ledger


# ---------------------------------------------------------------------------
# Git and hermes subprocess helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    error: Optional[str] = None

    def tail(self, limit: int = 200) -> str:
        text = (self.stderr or self.stdout or self.error or "").strip()
        return text[-limit:] if len(text) > limit else text


def _run(cmd: list[str], *, cwd: Optional[Path] = None, timeout: int) -> CommandResult:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean_subprocess_env(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(False, None, "", "", error=f"timed out after {timeout}s: {' '.join(cmd)}")
    except OSError as exc:
        return CommandResult(False, None, "", "", error=f"could not invoke {cmd[0]}: {exc}")
    return CommandResult(proc.returncode == 0, proc.returncode, proc.stdout, proc.stderr)


def _git(spec: HermesSettings, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> CommandResult:
    return _run(["git", "-C", str(spec.source_dir), *args], timeout=timeout)


def _hermes(spec: HermesSettings, *args: str, timeout: int) -> CommandResult:
    return _run([str(spec.bin_path), *args], cwd=spec.source_dir, timeout=timeout)


def checkout_present(spec: HermesSettings) -> Optional[str]:
    """None when HERMES_SOURCE_DIR is a git checkout, else why not."""
    if not spec.source_dir.is_dir():
        return f"HERMES_SOURCE_DIR {spec.source_dir} does not exist"
    if not (spec.source_dir / ".git").exists():
        return (
            f"{spec.source_dir} is not a git checkout (a Desktop bundle or "
            f"package install cannot be pinned; set HERMES_UPDATE_MODE=hermes-update "
            f"or point HERMES_SOURCE_DIR at the source checkout)"
        )
    return None


def list_upstream_tags(spec: HermesSettings) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Tag name to commit sha as origin has them right now.

    Annotated tags show up twice in `ls-remote`, once as the tag object and
    once peeled (`^{}`) to the commit; the peeled sha wins.
    """
    result = _git(spec, "ls-remote", "--tags", "origin")
    if not result.ok:
        return None, f"git ls-remote failed ({result.tail()})"
    tags: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        sha, ref = parts
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha
        else:
            tags[name] = sha
    for name, sha in peeled.items():
        tags[name] = sha
    return tags, None


def fetch_release_tags(spec: HermesSettings) -> Optional[str]:
    """Bring upstream `v*` tag objects and their commits into the checkout.

    They land under refs/mechanic/upstream-tags/, Mechanic's own namespace,
    force-updated to mirror origin. Hermes's refs/tags/ is never written,
    so nothing here can collide with a tag Hermes fetched itself, and a
    tag upstream moved simply mirrors; the ledger is what refuses it.
    Returns an error string or None.
    """
    result = _git(
        spec, "fetch", "--quiet", "--no-tags", "origin",
        f"+refs/tags/v*:{UPSTREAM_TAG_NAMESPACE}v*",
    )
    if result.ok:
        return None
    return f"git fetch failed ({result.tail()})"


def local_tag_dates(spec: HermesSettings) -> dict[str, datetime]:
    """Creation dates of the mirrored `v*` tags (tagger date, else commit date)."""
    result = _git(
        spec, "for-each-ref", "--format=%(refname) %(creatordate:iso-strict)",
        UPSTREAM_TAG_NAMESPACE,
    )
    dates: dict[str, datetime] = {}
    if not result.ok:
        return dates
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].startswith(UPSTREAM_TAG_NAMESPACE):
            continue
        name = parts[0][len(UPSTREAM_TAG_NAMESPACE):]
        stamp = _parse_time(parts[1].strip())
        if stamp is not None:
            dates[name] = stamp
    return dates


def head_commit(spec: HermesSettings) -> Optional[str]:
    result = _git(spec, "rev-parse", "HEAD")
    return result.stdout.strip() if result.ok and result.stdout.strip() else None


def describe_head(spec: HermesSettings, upstream: Optional[dict[str, str]] = None) -> str:
    """`v2026.9.24` when HEAD is exactly a tag, else `v2026.9.24+3.gabc1234`.

    Prefers the upstream tag map (a tag whose commit is HEAD names it), then
    falls back to `git describe` against Hermes's own tags.
    """
    head = head_commit(spec)
    if upstream and head:
        exact = [t for t, sha in upstream.items() if sha == head and RELEASE_TAG_RE.match(t)]
        if exact:
            return max(exact, key=version_key)
    # Release tags only: canary builds (v0.21.4+canary.<stamp>) and release
    # candidates (v2026.9.21-rc.1) also start with v and would win on a
    # real install (seen on the Mac mini, 2026-10-04).
    result = _git(
        spec, "describe", "--tags", "--long", "--match", "v*",
        "--exclude", "*+*", "--exclude", "*-*",
    )
    if not result.ok:
        return f"untagged ({(head or 'unknown')[:12]})"
    text = result.stdout.strip()
    match = re.match(r"^(?P<tag>.+)-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+)$", text)
    if not match:
        return text or "untagged"
    if match.group("distance") == "0":
        return match.group("tag")
    return f"{match.group('tag')}+{match.group('distance')}.g{match.group('sha')}"


def is_ancestor(spec: HermesSettings, ancestor: str, descendant: str) -> Optional[bool]:
    """git merge-base --is-ancestor; None when git could not say."""
    result = _git(spec, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.exit_code == 0:
        return True
    if result.exit_code == 1:
        return False
    return None


def worktree_clean(spec: HermesSettings) -> tuple[bool, str]:
    result = _git(spec, "status", "--porcelain", "--untracked-files=no")
    if not result.ok:
        return False, f"git status failed ({result.tail()})"
    dirty = result.stdout.strip()
    if dirty:
        return False, f"checkout has local changes:\n{dirty[:400]}"
    return True, ""


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HermesPlan:
    """What tonight should do about Hermes.

    `reason` is complete prose for the report and heartbeat. `error` is set
    when the facts could not be established (as opposed to established and
    still waiting). `code_before` is HEAD at planning time.
    """

    install: bool
    mode: str
    target_tag: Optional[str]
    target_commit: Optional[str]
    reason: str
    error: Optional[str] = None
    code_before: Optional[str] = None
    installed_label: Optional[str] = None
    next_tag: Optional[str] = None
    next_eligible_at: Optional[datetime] = None
    notes: tuple[str, ...] = ()


def scan_and_plan(
    spec: HermesSettings,
    *,
    now: Optional[datetime] = None,
    ledger_path: Optional[Path] = None,
) -> HermesPlan:
    """Talk to origin, update the ledger, and decide. Never raises."""
    now = now or datetime.now(timezone.utc)
    missing = checkout_present(spec)
    if missing is not None:
        return _blocked(spec, f"cannot plan: {missing}", error=missing)

    upstream, error = list_upstream_tags(spec)
    if upstream is None:
        return _blocked(spec, f"could not read release tags from origin ({error})", error=error)
    fetch_error = fetch_release_tags(spec)
    if fetch_error is not None:
        return _blocked(spec, f"could not fetch release tags ({fetch_error})", error=fetch_error)

    ledger = load_ledger(ledger_path)
    ledger = observe_tags(ledger, upstream, local_tag_dates(spec), now=now)
    try:
        save_ledger(ledger, ledger_path)
    except OSError as exc:
        return _blocked(spec, f"could not write the tag ledger ({exc})", error=str(exc))

    head = head_commit(spec)
    if head is None:
        return _blocked(spec, "could not read HEAD of the checkout", error="no HEAD")
    return plan_update(
        spec,
        ledger=ledger,
        upstream=upstream,
        head=head,
        installed_label=describe_head(spec, upstream),
        now=now,
        ancestor_fn=lambda a, b: is_ancestor(spec, a, b),
    )


def plan_update(
    spec: HermesSettings,
    *,
    ledger: TagLedger,
    upstream: dict[str, str],
    head: str,
    installed_label: str,
    now: datetime,
    ancestor_fn: Callable[[str, str], Optional[bool]],
) -> HermesPlan:
    """Pure decision from already-gathered facts (unit-testable)."""
    min_age = spec.min_update_age_days
    cutoff = now - timedelta(days=min_age)
    notes: list[str] = []

    release_tags = [t for t in upstream if RELEASE_TAG_RE.match(t) and t in ledger.tags]
    if not release_tags:
        return _blocked(spec, "origin has no release tags of the form vX.Y.Z", error="no release tags", head=head)

    eligible: list[str] = []
    pending: list[str] = []
    for tag in release_tags:
        record = ledger.tags[tag]
        if record.moved:
            notes.append(
                f"{tag} MOVED on origin ({record.commit[:12]} to "
                f"{(record.moved_to or '?')[:12]}); never installing it"
            )
            continue
        if tag in spec.skip_tags:
            notes.append(f"{tag} skipped (HERMES_SKIP_TAGS)")
            continue
        if min_age <= 0 or record.first_seen <= cutoff:
            eligible.append(tag)
        else:
            pending.append(tag)

    eligible.sort(key=version_key, reverse=True)
    newest_label = max(release_tags, key=version_key)

    def next_sentence(after: Optional[str]) -> tuple[str, Optional[str], Optional[datetime]]:
        upcoming = [
            t for t in pending
            if after is None or version_key(t) > version_key(after)
        ]
        if not upcoming:
            return "", None, None
        soonest = min(upcoming, key=lambda t: ledger.tags[t].first_seen)
        when = ledger.tags[soonest].first_seen + timedelta(days=min_age)
        return f" Next: {soonest} becomes eligible {_fmt_when(when)}.", soonest, when

    if not eligible:
        tail, nxt, when = next_sentence(None)
        reason = (
            f"no release tag has been in sight for {_days(min_age)} yet "
            f"(newest is {newest_label}); Mechanic waits.{tail}"
        )
        return HermesPlan(
            install=False, mode=spec.update_mode, target_tag=None, target_commit=None,
            reason=reason, code_before=head, installed_label=installed_label,
            next_tag=nxt, next_eligible_at=when, notes=tuple(notes),
        )

    target = eligible[0]
    record = ledger.tags[target]
    commit = upstream[target]
    age = _age_days(now, record.first_seen)
    seen_phrase = f"{target} (first seen {_fmt_date(record.first_seen)}, {_days(age)} ago)"
    tail, nxt, when = next_sentence(target)

    if commit == head:
        reason = (
            f"installed {installed_label} is already {target}, the newest release "
            f"at least {_days(min_age)} in sight.{tail}"
        )
        return HermesPlan(
            install=False, mode=spec.update_mode, target_tag=target, target_commit=commit,
            reason=reason, code_before=head, installed_label=installed_label,
            next_tag=nxt, next_eligible_at=when, notes=tuple(notes),
        )

    upgrade = ancestor_fn(head, commit)
    if upgrade is True:
        if spec.update_mode == MODE_HERMES_UPDATE:
            reason = (
                f"{seen_phrase} is the newest release old enough and newer than the "
                f"installed {installed_label}; running `hermes update --yes`, which "
                f"installs the tip of the {spec.update_channel or 'configured'} channel "
                f"(Hermes cannot pin a release).{tail}"
            )
        else:
            reason = (
                f"{seen_phrase} is the newest release old enough and newer than the "
                f"installed {installed_label}; pinning the checkout to it.{tail}"
            )
        return HermesPlan(
            install=True, mode=spec.update_mode, target_tag=target, target_commit=commit,
            reason=reason, code_before=head, installed_label=installed_label,
            next_tag=nxt, next_eligible_at=when, notes=tuple(notes),
        )

    ahead = ancestor_fn(commit, head)
    if ahead is True:
        reason = (
            f"installed {installed_label} is already ahead of {target}, the newest "
            f"release old enough (someone ran `hermes update`, or the checkout sits "
            f"on main's tip); Mechanic never downgrades and waits for a newer tag "
            f"to age.{tail}"
        )
        return HermesPlan(
            install=False, mode=spec.update_mode, target_tag=target, target_commit=commit,
            reason=reason, code_before=head, installed_label=installed_label,
            next_tag=nxt, next_eligible_at=when, notes=tuple(notes),
        )

    reason = (
        f"installed {installed_label} ({head[:12]}) and {target} ({commit[:12]}) are "
        f"on different lines of history, or git could not compare them; leaving the "
        f"checkout alone. Put it back on a release tag or on main to resume."
    )
    return HermesPlan(
        install=False, mode=spec.update_mode, target_tag=target, target_commit=commit,
        reason=reason, error="checkout diverged", code_before=head,
        installed_label=installed_label, next_tag=nxt, next_eligible_at=when,
        notes=tuple(notes),
    )


def _blocked(spec: HermesSettings, reason: str, *, error: str, head: Optional[str] = None) -> HermesPlan:
    return HermesPlan(
        install=False, mode=spec.update_mode, target_tag=None, target_commit=None,
        reason=reason + "; leaving the install alone.", error=error, code_before=head,
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of the install step, with every command's one-line verdict."""

    success: bool
    summary: str
    steps: list[str] = field(default_factory=list)
    code_after: Optional[str] = None
    rolled_back_code: bool = False


def quick_backup(spec: HermesSettings) -> str:
    """`hermes backup --quick`: Hermes's SQLite-safe copy of its state files.

    Best effort. Mechanic's own tar archive is the authoritative snapshot;
    this adds a consistent state.db copy inside it (see rollback.py).
    """
    result = _hermes(spec, "backup", "--quick", "--label", "mechanic", timeout=BACKUP_TIMEOUT_SECONDS)
    if result.ok:
        return "hermes backup --quick exit 0"
    return f"hermes backup --quick FAILED ({result.tail()})"


def apply_release_tag(spec: HermesSettings, plan: HermesPlan) -> ApplyResult:
    """Pin the checkout to plan.target_tag and rebuild its environment.

    On a dependency rebuild failure the checkout is put back on the commit
    it was on, and the environment rebuilt for that, so the running install
    is never left half-moved. Data is never touched here; that is the
    snapshot's job.
    """
    assert plan.target_tag and plan.target_commit and plan.code_before
    steps: list[str] = []

    clean, why = worktree_clean(spec)
    if not clean:
        return ApplyResult(False, f"refused: {why}", steps)

    checkout = _git(spec, "checkout", "--quiet", "--detach", plan.target_commit)
    if not checkout.ok:
        return ApplyResult(False, f"git checkout {plan.target_tag} failed ({checkout.tail()})", steps)
    steps.append(f"git checkout --detach {plan.target_tag} ({plan.target_commit[:12]}) exit 0")

    install = _hermes(spec, "pm", "install", timeout=PM_INSTALL_TIMEOUT_SECONDS)
    if install.ok:
        steps.append("hermes pm install exit 0")
        return ApplyResult(
            True, f"pinned to {plan.target_tag}; hermes pm install exit 0", steps,
            code_after=plan.target_commit,
        )

    steps.append(f"hermes pm install FAILED ({install.tail()})")
    _LOG.error(
        "hermes: pm install failed after pinning %s; putting the checkout back on %s\n"
        "----- stdout -----\n%s\n----- stderr -----\n%s\n------------------",
        plan.target_tag, plan.code_before[:12], install.stdout or "<empty>", install.stderr or "<empty>",
    )
    back = _git(spec, "checkout", "--quiet", "--detach", plan.code_before)
    if not back.ok:
        steps.append(f"rollback checkout FAILED ({back.tail()})")
        return ApplyResult(
            False,
            f"hermes pm install failed and the checkout could not be put back on "
            f"{plan.code_before[:12]}; Hermes may be unusable until you run "
            f"`git -C {spec.source_dir} checkout --detach {plan.code_before}` and "
            f"`hermes pm install`",
            steps, code_after=plan.target_commit,
        )
    steps.append(f"rollback: git checkout --detach {plan.code_before[:12]} exit 0")
    reinstall = _hermes(spec, "pm", "install", timeout=PM_INSTALL_TIMEOUT_SECONDS)
    steps.append(
        "rollback: hermes pm install exit 0" if reinstall.ok
        else f"rollback: hermes pm install FAILED ({reinstall.tail()})"
    )
    return ApplyResult(
        False,
        f"hermes pm install failed for {plan.target_tag}; checkout put back on "
        f"{plan.code_before[:12]}" + ("" if reinstall.ok else " but its environment could not be rebuilt"),
        steps, code_after=plan.code_before, rolled_back_code=True,
    )


def apply_hermes_update(spec: HermesSettings, plan: HermesPlan) -> ApplyResult:
    """`hermes update --yes [--channel X]`: Hermes's own updater, unattended."""
    args = ["update", "--yes"]
    if spec.update_channel:
        args += ["--channel", spec.update_channel]
    result = _hermes(spec, *args, timeout=HERMES_UPDATE_TIMEOUT_SECONDS)
    label = "hermes " + " ".join(args)
    if result.ok:
        receipt = read_update_receipt(spec)
        outcome = receipt.get("outcome") if receipt else None
        summary = f"{label} exit 0" + (f" (receipt outcome: {outcome})" if outcome else "")
        return ApplyResult(outcome in (None, "success"), summary, [summary], code_after=head_commit(spec))
    _LOG.error(
        "hermes: %s exit %s\n----- stdout -----\n%s\n----- stderr -----\n%s\n------------------",
        label, result.exit_code, result.stdout or "<empty>", result.stderr or "<empty>",
    )
    summary = f"{label} exit {result.exit_code}: {result.tail()}"
    return ApplyResult(False, summary, [summary], code_after=head_commit(spec))


def read_update_receipt(spec: HermesSettings) -> Optional[dict]:
    """The latest `hermes update` receipt, if Hermes wrote one."""
    path = spec.home / "logs" / "update_receipts" / "latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_doctor(spec: HermesSettings) -> tuple[str, bool]:
    """`hermes doctor --fix` (HERMES_DOCTOR_FIX) or read-only `hermes doctor`.

    `--fix` applies safe config migrations unattended (the same path
    `hermes update` takes without a terminal) and may install a macOS TCC
    helper or acquire a missing tool through PM. Exit code is
    informational; the verify step is the authority.
    """
    args = ["doctor", "--fix"] if spec.doctor_fix else ["doctor"]
    result = _hermes(spec, *args, timeout=DOCTOR_TIMEOUT_SECONDS)
    label = "hermes " + " ".join(args)
    if result.ok:
        return f"{label} exit 0", True
    if result.error:
        return f"{label} {result.error}", False
    return f"{label} exit {result.exit_code}: {result.tail()}", False


def pm_status_ok(spec: HermesSettings) -> tuple[bool, str]:
    """`hermes pm status`: the last dependency-sync receipt, machine-readable."""
    result = _hermes(spec, "pm", "status", timeout=120)
    if not result.ok:
        return False, f"hermes pm status exit {result.exit_code}: {result.tail()}"
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return True, "hermes pm status exit 0"
    outcome = data.get("outcome") if isinstance(data, dict) else None
    if outcome and outcome not in ("success", "ok"):
        return False, f"hermes pm status reports outcome {outcome!r}"
    return True, "hermes pm status exit 0" + (f" (outcome {outcome})" if outcome else "")


def gateway_restart(spec: HermesSettings) -> str:
    """`hermes gateway restart`: drain-first restart through launchd."""
    result = _hermes(spec, "gateway", "restart", timeout=spec.gateway_restart_timeout_seconds)
    if result.ok:
        return "hermes gateway restart exit 0"
    if result.error:
        return f"hermes gateway restart {result.error}"
    return f"hermes gateway restart exit {result.exit_code}: {result.tail()}"


def gateway_status_ok(spec: HermesSettings) -> tuple[bool, str]:
    result = _hermes(spec, "gateway", "status", timeout=60)
    if result.ok:
        return True, "hermes gateway status exit 0"
    return False, f"hermes gateway status exit {result.exit_code}: {result.tail()}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _age_days(now: datetime, stamp: datetime) -> int:
    return max(0, int((now - stamp).total_seconds() // 86400))


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def _fmt_date(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _fmt_when(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _fmt_iso(stamp: Optional[datetime]) -> Optional[str]:
    if stamp is None:
        return None
    return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
