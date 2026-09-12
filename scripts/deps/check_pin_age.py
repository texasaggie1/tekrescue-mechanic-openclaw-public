#!/usr/bin/env python3
"""Check that every pin in requirements.txt is old enough to trust.

Same policy as MIN_UPDATE_AGE_DAYS for OpenClaw, applied to Mechanic's own
Python dependencies: a release has to have been on PyPI for at least
MIN_AGE_DAYS (default 7) before we pin to it, so a poisoned release has had
time to be noticed and yanked. This script is the independent check after
scripts/deps/relock.sh (which uses uv's own cutoff): it asks PyPI directly
for each pinned version's upload time and yank status, and exits non-zero
if any pin is too young or yanked.

Usage:
    python3 scripts/deps/check_pin_age.py                 # checks requirements.txt
    python3 scripts/deps/check_pin_age.py --min-age-days 14
    python3 scripts/deps/check_pin_age.py path/to/other-requirements.txt

Standard library only, on purpose: it must run before the pins it checks
are installed. Honours HTTPS_PROXY and SSL_CERT_FILE like any urllib client.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PYPI_URL = "https://pypi.org/pypi/{name}/json"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9.!+_-]+)")


@dataclass(frozen=True)
class PinReport:
    name: str
    version: str
    uploaded: Optional[datetime]
    yanked: bool
    newest_eligible: Optional[str]
    error: Optional[str] = None

    def age_days(self, now: datetime) -> Optional[int]:
        if self.uploaded is None:
            return None
        return int((now - self.uploaded).total_seconds() // 86400)


def parse_pins(path: Path) -> list[tuple[str, str]]:
    """Return (name, version) for every `name==version` line in the file.

    Hash continuation lines, comments, options, markers, and editable
    entries are skipped. A name may appear once per file.
    """
    pins: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = _PIN.match(line)
        if match:
            pins.append((match.group(1).lower().replace("_", "-"), match.group(2)))
    return pins


def fetch_project(name: str, timeout: float) -> dict:
    request = urllib.request.Request(
        PYPI_URL.format(name=name),
        headers={"Accept": "application/json", "User-Agent": "tekrescue-mechanic-check-pin-age"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _upload_time(files: list[dict]) -> Optional[datetime]:
    stamps = []
    for entry in files:
        raw = entry.get("upload_time_iso_8601") or entry.get("upload_time")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamps.append(parsed.astimezone(timezone.utc))
    return min(stamps) if stamps else None


def _is_prerelease(version: str) -> bool:
    return bool(re.search(r"(a|b|rc|dev|alpha|beta|pre|preview)\d*", version, re.IGNORECASE))


def inspect_pin(name: str, version: str, *, now: datetime, min_age_days: int, timeout: float) -> PinReport:
    try:
        project = fetch_project(name, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return PinReport(name, version, None, False, None, error=f"PyPI lookup failed: {exc}")

    releases = project.get("releases") or {}
    files = releases.get(version)
    if not files:
        return PinReport(name, version, None, False, None, error="version not found on PyPI")
    uploaded = _upload_time(files)
    yanked = any(entry.get("yanked") for entry in files)

    # The newest release that would pass the policy today, so a bump session
    # knows what it may move to without re-deriving it.
    newest_eligible: Optional[str] = None
    newest_stamp: Optional[datetime] = None
    for candidate, candidate_files in releases.items():
        if not candidate_files or _is_prerelease(candidate):
            continue
        if any(entry.get("yanked") for entry in candidate_files):
            continue
        stamp = _upload_time(candidate_files)
        if stamp is None or (now - stamp).days < min_age_days:
            continue
        if newest_stamp is None or stamp > newest_stamp:
            newest_eligible, newest_stamp = candidate, stamp

    return PinReport(name, version, uploaded, yanked, newest_eligible)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "requirements",
        nargs="?",
        default=str(Path(__file__).resolve().parents[2] / "requirements.txt"),
        help="Requirements file to check (default: the repo's requirements.txt).",
    )
    parser.add_argument("--min-age-days", type=int, default=7, help="Minimum age a pinned release must have (default 7).")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds (default 30).")
    args = parser.parse_args(argv)

    path = Path(args.requirements)
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    pins = parse_pins(path)
    if not pins:
        print(f"no `name==version` pins found in {path}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    print(f"Checking {len(pins)} pins in {path} against PyPI (minimum age {args.min_age_days} days)\n")
    print(f"{'package':22} {'pinned':14} {'uploaded':12} {'age':>6}  {'newest eligible':16} verdict")
    failures = 0
    for name, version in pins:
        report = inspect_pin(name, version, now=now, min_age_days=args.min_age_days, timeout=args.timeout)
        if report.error:
            verdict = f"ERROR: {report.error}"
            failures += 1
            uploaded, age = "?", "?"
        else:
            days = report.age_days(now)
            uploaded = report.uploaded.strftime("%Y-%m-%d") if report.uploaded else "?"
            age = f"{days}d" if days is not None else "?"
            if report.yanked:
                verdict = "FAIL: yanked on PyPI"
                failures += 1
            elif days is None or days < args.min_age_days:
                verdict = f"FAIL: younger than {args.min_age_days} days"
                failures += 1
            else:
                verdict = "ok"
        newest = report.newest_eligible or "?"
        if report.newest_eligible and report.newest_eligible != version and not report.error:
            newest += " (bump?)"
        print(f"{name:22} {version:14} {uploaded:12} {age:>6}  {newest:16} {verdict}")

    print()
    if failures:
        print(f"{failures} pin(s) failed the policy. Fix requirements.txt before shipping it.")
        return 1
    print("All pins pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
