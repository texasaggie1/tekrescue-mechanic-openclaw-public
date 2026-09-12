"""Release waiting period for OpenClaw updates (new in v0.1.4).

Supply-chain quarantine. OpenClaw ships a new release every two or three
days from a repository with hundreds of contributors. A compromised
release (a poisoned commit on main, a hijacked publish token, a bad
dependency pulled in at build time) is usually noticed and pulled within
days of publication, so the cheapest defence is to not be first. Mechanic
therefore refuses to install any release until it has been public on the
npm registry for at least MIN_UPDATE_AGE_DAYS (default 7).

Waiting for "the newest release" to turn a week old would mean never
updating, because a newer one lands first. Instead Mechanic installs the
newest release that IS old enough, via `openclaw update --yes --tag
<version>`, and walks the release train a week behind the head: each
nightly installs the next version that has crossed the age line.

How the target is chosen on the stable channel:

  1. One read-only registry call, `npm view openclaw time versions
     dist-tags --json`, through the operator's own npm so that registry
     mirrors, proxies, and .npmrc auth are honoured. (npm's `--before`
     date filter applies to `npm install` only; `npm view` ignores it,
     checked 2026-09-12, so the filtering happens here.)
  2. Candidates are versions that are currently published, carry no
     prerelease suffix (2026.9.1-beta.1 and the hotfix-style 2026.2.2-1
     are both excluded), sort no higher than the `latest` dist-tag, and
     were published on or before now minus MIN_UPDATE_AGE_DAYS.
  3. The highest candidate by version order is the target. It is
     installed only when it is newer than what is installed; otherwise
     Mechanic waits and says when the next version becomes eligible.

Other channels (beta, extended-stable, dev) keep their version selection
inside OpenClaw: Mechanic waits for the version OpenClaw itself reports as
available to cross the age line, then updates to exactly that version
(plain `openclaw update --yes` on extended-stable and dev, where OpenClaw
does not accept `--tag`).

Any failure to establish dates (npm missing, registry unreachable, the
reported version absent from the registry, installed version unknown)
resolves to "do not install", Mechanic's safe default everywhere.
MIN_UPDATE_AGE_DAYS=0 turns the waiting period off and restores the
v0.1.3 behaviour of installing whatever `openclaw update` picks.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import Config, clean_subprocess_env
from .verifier import UpdateAvailability


_LOG = logging.getLogger(__name__)

PACKAGE_NAME = "openclaw"
NPM_VIEW_TIMEOUT_SECONDS = 60
STABLE_CHANNEL = "stable"

# Channels on which OpenClaw refuses (extended-stable) or does not
# meaningfully support (dev, a git checkout) an explicit `--tag`.
_PLAIN_UPDATE_CHANNELS = ("extended-stable", "dev")

_VERSION_CORE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass(frozen=True)
class RegistrySnapshot:
    """What the npm registry says about OpenClaw right now.

    `published` maps every currently published version to its publish
    time (UTC). `latest` is the `latest` dist-tag, the ceiling for stable
    candidates. `npm_path` records which npm answered, for the log.
    """

    published: dict[str, datetime]
    latest: Optional[str]
    npm_path: Path


@dataclass(frozen=True)
class UpdatePlan:
    """What the nightly should do about an available update.

    `install` False means "leave the install alone tonight". `reason` is a
    complete sentence (or two) that the morning report and the heartbeat
    show verbatim, so it always says what happened and what comes next.
    `use_tag` says whether the install must pin `target_version` with
    `--tag`; when False the plain `openclaw update --yes` path is used.
    `error` is set only when the waiting period could not be applied (as
    opposed to applied and still waiting).
    """

    install: bool
    target_version: Optional[str]
    use_tag: bool
    reason: str
    error: Optional[str] = None
    next_version: Optional[str] = None
    next_eligible_at: Optional[datetime] = None


def find_npm(config: Config) -> Optional[Path]:
    """Locate npm: next to the openclaw binary first, then on PATH.

    The sibling wins because it belongs to the same Node install OpenClaw
    runs under (Homebrew, nvm, a Node.js pkg all lay them out together).
    PATH is the one the subprocess would see, i.e. the clean environment.
    """
    sibling = config.openclaw_bin_path.parent / "npm"
    if sibling.is_file():
        return sibling
    found = shutil.which("npm", path=clean_subprocess_env().get("PATH"))
    return Path(found) if found else None


def fetch_registry_snapshot(
    config: Config,
) -> tuple[Optional[RegistrySnapshot], Optional[str]]:
    """Ask the npm registry for OpenClaw's publish times, versions, and tags.

    Returns (snapshot, None) on success or (None, error) on any failure.
    Read-only; nothing on the machine changes. Goes through npm rather
    than a direct HTTP call so the operator's registry configuration
    (mirror, proxy, auth token) applies exactly as it does for
    `openclaw update` itself.
    """
    npm = find_npm(config)
    if npm is None:
        return None, (
            f"npm not found next to {config.openclaw_bin_path} or on PATH"
        )
    cmd = [str(npm), "view", PACKAGE_NAME, "time", "versions", "dist-tags", "--json"]
    _LOG.debug("release_age: running %s", cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=clean_subprocess_env(),
            timeout=NPM_VIEW_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, (
            f"`npm view {PACKAGE_NAME}` timed out after {NPM_VIEW_TIMEOUT_SECONDS}s"
        )
    except OSError as exc:
        return None, f"could not invoke {npm}: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:200]
        return None, f"`npm view {PACKAGE_NAME}` exit {proc.returncode}: {detail}"

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, f"`npm view {PACKAGE_NAME}` emitted non-JSON: {exc}"

    return parse_registry_payload(payload, npm_path=npm)


def parse_registry_payload(
    payload: object, *, npm_path: Path
) -> tuple[Optional[RegistrySnapshot], Optional[str]]:
    """Turn the `npm view ... --json` object into a RegistrySnapshot.

    Only versions present in BOTH `versions` (currently published) and
    `time` (has a publish stamp) count. npm collapses a single-element
    `versions` array into a bare string; that is handled too.
    """
    if not isinstance(payload, dict):
        return None, "registry response was not a JSON object"
    time_map = payload.get("time")
    versions = payload.get("versions")
    tags = payload.get("dist-tags")
    if not isinstance(time_map, dict):
        return None, "registry response has no publish times"
    if isinstance(versions, str):
        versions = [versions]
    if not isinstance(versions, list):
        return None, "registry response has no version list"

    published: dict[str, datetime] = {}
    for version in versions:
        if not isinstance(version, str):
            continue
        stamp = _parse_timestamp(time_map.get(version))
        if stamp is not None:
            published[version] = stamp
    if not published:
        return None, "registry response listed no published versions with dates"

    latest = tags.get("latest") if isinstance(tags, dict) else None
    snapshot = RegistrySnapshot(
        published=published,
        latest=latest if isinstance(latest, str) and latest else None,
        npm_path=npm_path,
    )
    return snapshot, None


def plan_update(
    config: Config,
    *,
    installed_version: Optional[str],
    availability: UpdateAvailability,
    now: Optional[datetime] = None,
    snapshot: Optional[RegistrySnapshot] = None,
) -> UpdatePlan:
    """Decide what to install tonight, given that OpenClaw reports an update.

    Args:
        config: Loaded configuration; `min_update_age_days` is the policy.
        installed_version: What `openclaw --version` reported, or None.
        availability: A successful `check_for_updates` result with
            `available=True`.
        now: Injectable clock (UTC) for tests. Defaults to the real one.
        snapshot: Injectable registry snapshot for tests. When None, the
            registry is queried through npm.

    Returns:
        An UpdatePlan. Never raises; every failure becomes a plan with
        `install=False` and a human-readable `reason`.
    """
    min_age = config.min_update_age_days
    latest = availability.latest_version
    if min_age <= 0:
        return UpdatePlan(
            install=True,
            target_version=latest,
            use_tag=False,
            reason="no waiting period configured (MIN_UPDATE_AGE_DAYS=0)",
        )

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=min_age)

    if snapshot is None:
        snapshot, error = fetch_registry_snapshot(config)
        if snapshot is None:
            what = latest or "the update"
            return UpdatePlan(
                install=False,
                target_version=None,
                use_tag=False,
                reason=(
                    f"could not confirm release dates ({error}), so {what} "
                    f"stays uninstalled until Mechanic can. Set "
                    f"MIN_UPDATE_AGE_DAYS=0 to turn the waiting period off."
                ),
                error=error,
            )

    channel = (availability.channel or STABLE_CHANNEL).lower()
    if channel == STABLE_CHANNEL:
        return _plan_stable(
            snapshot, installed_version=installed_version, latest=latest,
            min_age=min_age, now=now, cutoff=cutoff,
        )
    return _plan_other_channel(
        snapshot, channel=channel, latest=latest,
        min_age=min_age, now=now, cutoff=cutoff,
    )


def _plan_stable(
    snapshot: RegistrySnapshot,
    *,
    installed_version: Optional[str],
    latest: Optional[str],
    min_age: int,
    now: datetime,
    cutoff: datetime,
) -> UpdatePlan:
    ceiling = snapshot.latest or latest
    ceiling_key = version_key(ceiling) if ceiling else None

    def in_scope(version: str) -> bool:
        if is_prerelease(version):
            return False
        return ceiling_key is None or version_key(version) <= ceiling_key

    eligible = [
        v for v, stamp in snapshot.published.items()
        if in_scope(v) and stamp <= cutoff
    ]
    if not eligible:
        return UpdatePlan(
            install=False,
            target_version=None,
            use_tag=False,
            reason=(
                f"no OpenClaw release on the registry is {_days(min_age)} old "
                f"yet; Mechanic waits until one is."
            ),
        )
    target = max(eligible, key=version_key)

    # The release that will next cross the age line AND would move the
    # target: the earliest-published version newer than the target.
    upcoming = [
        v for v, stamp in snapshot.published.items()
        if in_scope(v) and stamp > cutoff and version_key(v) > version_key(target)
    ]
    next_version = min(upcoming, key=lambda v: snapshot.published[v]) if upcoming else None
    next_eligible_at = (
        snapshot.published[next_version] + timedelta(days=min_age)
        if next_version else None
    )
    next_sentence = (
        f" Next: {next_version} becomes eligible {_fmt_when(next_eligible_at)}."
        if next_version and next_eligible_at else ""
    )
    latest_phrase = (
        _describe(latest, snapshot, now) if latest and latest != target else None
    )

    if installed_version is None:
        return UpdatePlan(
            install=False,
            target_version=None,
            use_tag=False,
            reason=(
                f"installed OpenClaw version is unknown, so Mechanic cannot "
                f"tell whether {target} would be an upgrade; leaving the "
                f"install alone."
            ),
            error="installed version unknown",
            next_version=next_version,
            next_eligible_at=next_eligible_at,
        )

    if version_key(target) <= version_key(installed_version):
        lead = (
            f"{latest_phrase} is too new; Mechanic waits until a release is "
            f"{_days(min_age)} old."
            if latest_phrase else
            f"Mechanic waits until a release is {_days(min_age)} old."
        )
        if version_key(target) == version_key(installed_version):
            standing = (
                f" Installed {installed_version} is already the newest "
                f"release old enough."
            )
        else:
            standing = (
                f" Installed {installed_version} is newer than {target}, the "
                f"newest release old enough, so nothing changes."
            )
        return UpdatePlan(
            install=False,
            target_version=None,
            use_tag=False,
            reason=lead + standing + next_sentence,
            next_version=next_version,
            next_eligible_at=next_eligible_at,
        )

    reason = (
        f"{_describe(target, snapshot, now)} is the newest release at least "
        f"{_days(min_age)} old"
    )
    if latest_phrase:
        reason += f"; {latest_phrase} waits"
    reason += "." + next_sentence
    return UpdatePlan(
        install=True,
        target_version=target,
        use_tag=True,
        reason=reason,
        next_version=next_version,
        next_eligible_at=next_eligible_at,
    )


def _plan_other_channel(
    snapshot: RegistrySnapshot,
    *,
    channel: str,
    latest: Optional[str],
    min_age: int,
    now: datetime,
    cutoff: datetime,
) -> UpdatePlan:
    if not latest:
        return UpdatePlan(
            install=False,
            target_version=None,
            use_tag=False,
            reason=(
                f"OpenClaw reports an update on the {channel} channel without "
                f"naming a version, so its age cannot be confirmed; leaving the "
                f"install alone."
            ),
            error="no version reported",
        )
    published_at = snapshot.published.get(latest)
    if published_at is None:
        return UpdatePlan(
            install=False,
            target_version=None,
            use_tag=False,
            reason=(
                f"{latest} ({channel} channel) is not on the npm registry, so "
                f"its age cannot be confirmed; leaving the install alone."
            ),
            error="version not on registry",
        )
    if published_at > cutoff:
        eligible_at = published_at + timedelta(days=min_age)
        return UpdatePlan(
            install=False,
            target_version=None,
            use_tag=False,
            reason=(
                f"{_describe(latest, snapshot, now)} on the {channel} channel is "
                f"too new; Mechanic waits until a release is {_days(min_age)} "
                f"old. It becomes eligible {_fmt_when(eligible_at)}."
            ),
            next_version=latest,
            next_eligible_at=eligible_at,
        )
    return UpdatePlan(
        install=True,
        target_version=latest,
        use_tag=channel not in _PLAIN_UPDATE_CHANNELS,
        reason=(
            f"{_describe(latest, snapshot, now)} on the {channel} channel is "
            f"past the {_days(min_age)} waiting period."
        ),
    )


def version_key(version: str) -> tuple[tuple[int, int, int], int, tuple]:
    """Sort key implementing semver precedence for OpenClaw's versions.

    OpenClaw's calendar versions (2026.9.4) are valid semver, so the usual
    rules apply: numeric major.minor.patch, and a version with a
    prerelease suffix (2026.9.1-beta.1, 2026.2.2-1) sorts BELOW the bare
    version. Build metadata after `+` is ignored. Unparseable input sorts
    lowest rather than raising, because the caller is an unattended job.
    """
    core, _, rest = version.partition("-")
    core = core.split("+", 1)[0]
    match = _VERSION_CORE.match(core.strip())
    if match:
        numbers = tuple(int(group) if group else 0 for group in match.groups())
    else:
        numbers = (0, 0, 0)
    prerelease = rest.split("+", 1)[0]
    if not prerelease:
        return (numbers, 1, ())
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in prerelease.split(".")
    )
    return (numbers, 0, identifiers)


def is_prerelease(version: str) -> bool:
    """True for anything with a `-suffix` (betas, hotfix-style -1, -2)."""
    return "-" in version.split("+", 1)[0]


def _describe(version: str, snapshot: RegistrySnapshot, now: datetime) -> str:
    stamp = snapshot.published.get(version)
    if stamp is None:
        return f"{version} (publish date unknown)"
    return f"{version} (published {_fmt_date(stamp)}, {_days(_age_days(now, stamp))} old)"


def _age_days(now: datetime, stamp: datetime) -> int:
    return max(0, int((now - stamp).total_seconds() // 86400))


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def _fmt_date(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _fmt_when(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _parse_timestamp(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
