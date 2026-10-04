#!/usr/bin/env bash
# Pull the product files from the public checkout in ./public into this
# (upstream) checkout, keeping the deltas the upstream copy owns.
#
# The maintainer's development checkout keeps the public repo checked out
# as a `public/` subfolder. Normally changes flow upstream -> public. When
# a release is built in the public repo first (v0.2.0 was), this script
# brings the upstream checkout level with it:
#
#   mechanic/ tests/ scripts/deps/        mirrored (extra files removed)
#   install.sh uninstall.sh .env.example  copied
#   LICENSE requirements.txt AGENTS.md    copied
#   pyproject.toml                        copied; Homepage/Issues keep
#                                         THIS checkout's origin URL
#   .gitignore                            copied; `public/` stays ignored
#   README.md                             copied; repo links point at THIS
#                                         checkout's origin; the two
#                                         public-only blocks (the AI-tool
#                                         recommendation under Quickstart
#                                         and "Working on this with an AI
#                                         assistant") are removed
#   scripts/recovery/*                    existing files are left alone
#                                         (the upstream copies are the
#                                         personalised originals); only
#                                         missing files are added
#
# Nothing is committed. Review `git status` and `git diff`, then commit
# with the upstream's own identity. No em dashes anywhere, house rule.
set -euo pipefail

if [ ! -d public/.git ]; then
  echo "sync-from-public: run this from the upstream checkout root; ./public must be the public checkout" >&2
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "sync-from-public: this checkout has uncommitted changes to tracked files; commit or stash them first" >&2
  exit 1
fi

repo_url() {  # normalise an origin URL to https://host/owner/repo
  local url="$1"
  url="${url%.git}"
  case "$url" in
    git@*:*) url="https://${url#git@}"; url="${url/:/\/}" ;;
  esac
  printf '%s' "$url"
}
UP_URL="$(repo_url "$(git remote get-url origin)")"
PUB_URL="$(repo_url "$(git -C public remote get-url origin)")"
UP_NAME="${UP_URL##*/}"
PUB_NAME="${PUB_URL##*/}"
echo "upstream: $UP_URL"
echo "public:   $PUB_URL"
echo "public at $(git -C public rev-parse --short HEAD) ($(git -C public rev-parse --abbrev-ref HEAD))"
echo

for dir in mechanic tests scripts/deps; do
  rm -rf "$dir"
  mkdir -p "$(dirname "$dir")"
  cp -R "public/$dir" "$dir"
  find "$dir" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$dir" -name '*.pyc' -delete 2>/dev/null || true
  echo "mirrored  $dir/"
done

for file in install.sh uninstall.sh .env.example LICENSE requirements.txt AGENTS.md; do
  cp "public/$file" "$file"
  echo "copied    $file"
done

cp public/pyproject.toml pyproject.toml
python3 - "$PUB_URL" "$UP_URL" <<'PY'
import sys
from pathlib import Path
pub, up = sys.argv[1], sys.argv[2]
p = Path("pyproject.toml")
p.write_text(p.read_text().replace(pub, up))
PY
echo "copied    pyproject.toml (project URLs point at $UP_NAME)"

cp public/.gitignore .gitignore
grep -qx 'public/' .gitignore || printf '\n# The public repo, checked out as a subfolder (its own git repo).\npublic/\n' >> .gitignore
echo "copied    .gitignore (public/ stays ignored)"

cp public/README.md README.md
python3 - "$PUB_URL" "$UP_URL" "$PUB_NAME" "$UP_NAME" <<'PY'
import re, sys
from pathlib import Path
pub, up, pub_name, up_name = sys.argv[1:5]
p = Path("README.md")
text = p.read_text()
text = text.replace(pub, up)
text = text.replace(pub.removeprefix("https://"), up.removeprefix("https://"))
text = text.replace(f"cd {pub_name}", f"cd {up_name}")
# The AI-tool recommendation paragraph under Quickstart: from its first
# line to the blank line that ends it.
text = re.sub(r"\nHonest recommendation:.*?\n\n", "\n", text, count=1, flags=re.S)
# The whole "Working on this with an AI assistant" section.
text = re.sub(r"\n## Working on this with an AI assistant\n.*?(?=\n## )", "", text, count=1, flags=re.S)
p.write_text(text)
PY
echo "copied    README.md (links point at $UP_NAME; public-only blocks removed)"

mkdir -p scripts/recovery
for src in public/scripts/recovery/*; do
  name="$(basename "$src")"
  if [ -e "scripts/recovery/$name" ]; then
    echo "kept      scripts/recovery/$name (upstream copy is the original; diff against public/ by hand)"
  else
    cp "$src" "scripts/recovery/$name"
    echo "added     scripts/recovery/$name"
  fi
done

echo
echo "Done. Nothing committed. Review with: git status; git diff --stat"
git status --short | head -40
