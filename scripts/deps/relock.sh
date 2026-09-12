#!/usr/bin/env bash
# Regenerate requirements.txt: every Python package Mechanic installs, pinned
# to an exact version with sha256 hashes, and never to a release younger
# than MIN_AGE_DAYS (default 7). Same reasoning as MIN_UPDATE_AGE_DAYS for
# OpenClaw: a poisoned release is usually caught within days, so we do not
# take releases the day they land.
#
# Usage:
#   scripts/deps/relock.sh                 # cutoff = now minus 7 days
#   MIN_AGE_DAYS=14 scripts/deps/relock.sh # a longer quarantine
#
# Requires uv (https://docs.astral.sh/uv/): `brew install uv` or `pip install uv`.
# uv's --exclude-newer is what enforces the age cutoff during resolution.
# Afterwards, scripts/deps/check_pin_age.py verifies the result against
# PyPI's own upload dates, independently of uv.
#
# Runtime dependencies come from pyproject.toml. The build tools pip needs
# for `pip install --no-build-isolation -e .` (setuptools, wheel) are added
# on stdin so they get pinned too; otherwise pip would download the newest
# setuptools, unpinned, at install time.

set -euo pipefail

MIN_AGE_DAYS="${MIN_AGE_DAYS:-7}"
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${REPO_DIR}/requirements.txt"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required: brew install uv (or pip install uv)" 1>&2
    exit 1
fi

CUTOFF="$(python3 -c "import datetime as d; print((d.datetime.now(d.timezone.utc) - d.timedelta(days=${MIN_AGE_DAYS})).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
TODAY="$(date -u +%Y-%m-%d)"
TMP="$(mktemp)"
trap 'rm -f "${TMP}"' EXIT

printf 'setuptools>=68\nwheel\n' | uv pip compile \
    "${REPO_DIR}/pyproject.toml" - \
    --generate-hashes \
    --universal \
    --python-version 3.11 \
    --exclude-newer "${CUTOFF}" \
    --no-header \
    --quiet \
    -o "${TMP}"

{
    cat <<HEADER
# tekRESCUE Mechanic: locked Python dependencies.
#
# Every package Mechanic installs, pinned to an exact version with sha256
# hashes. Install with:
#
#     pip install --require-hashes -r requirements.txt
#     pip install --no-build-isolation --no-deps -e .
#
# Regenerate with scripts/deps/relock.sh, which refuses any release younger
# than MIN_AGE_DAYS (default 7), then verify with scripts/deps/check_pin_age.py.
# Do not edit by hand.
#
# Generated ${TODAY} with releases published on or before ${CUTOFF}.
HEADER
    cat "${TMP}"
} > "${OUT}"

echo "wrote ${OUT} (cutoff ${CUTOFF})"
echo "now run: python3 scripts/deps/check_pin_age.py"
