#!/usr/bin/env bash
# fix-openclaw-node22.sh
# Recovery for Incident 2026-07 followup: `openclaw update` blocked by
# a Node runtime version mismatch on the managed gateway service.
# Bumps Homebrew's node@22 in place so openclaw@2026.7.1-2 satisfies its
# engine constraint (>=22.22.3 on the 22.x line), then restarts the
# gateway and retries `openclaw update`.
#
# Follows the maintenance standing orders (see AGENTS.md): snapshot before
# surgery, diff-and-approve per mutation, post-op verify, maintenance
# report.
#
# Usage:
#   chmod +x scripts/recovery/fix-openclaw-node22.sh
#   ./scripts/recovery/fix-openclaw-node22.sh

set -euo pipefail
IFS=$'\n\t'

REPORT_DATE="$(date +%Y-%m-%d)"
REPORT_DIR="$HOME/.openclaw/workspace/reports"
REPORT_PATH="$REPORT_DIR/maintenance-${REPORT_DATE}.md"
mkdir -p "$REPORT_DIR"

confirm() {
    local ans
    read -r -p "$1 [y/N] " ans
    case "${ans:-N}" in
        y|Y|yes|YES) : ;;
        *) echo "Aborted." ; exit 1 ;;
    esac
}

echo "==============================================================="
echo "  OpenClaw node@22 runtime upgrade"
echo "  Follows the maintenance standing orders (see AGENTS.md)"
echo "  Report will be written to:"
echo "    $REPORT_PATH"
echo "==============================================================="

# --- Step 1: pre-check Homebrew's current node@22 formula version ---
echo
echo ">>> Step 1/8: Homebrew node@22 formula version"
brew info node@22 | head -3
echo
echo "    The formula version above must be >= 22.22.3."
confirm "    Is it?"

# --- Step 2: record current pinned Node version (rollback baseline) ---
echo
echo ">>> Step 2/8: record current pinned node@22 version"
if NODE_BEFORE_RAW="$(/opt/homebrew/opt/node@22/bin/node --version 2>&1)"; then
    NODE_BEFORE="$NODE_BEFORE_RAW"
    echo "    current pinned version: $NODE_BEFORE"
else
    NODE_BEFORE="BROKEN"
    echo "    current pinned node@22 binary is BROKEN and cannot report a version:"
    echo "$NODE_BEFORE_RAW" | sed 's/^/      /' | head -5
    echo
    echo "    This is expected if Homebrew has upgraded a dependency (e.g. simdjson)"
    echo "    without relinking node@22. The 'brew upgrade node@22' in step 4 will"
    echo "    fix it. There is no meaningful rollback baseline to capture from a"
    echo "    binary that is already broken; continuing without one is acceptable."
    confirm "    Proceed anyway?"
fi

# --- Step 3: pre-maintenance snapshot (Rule 1) ---
echo
echo ">>> Step 3/8: snapshot OpenClaw workspace (Rule 1)"
SNAPSHOT_REF="none"
if [ -d "$HOME/.openclaw/workspace/.git" ]; then
    ( cd "$HOME/.openclaw/workspace"
      git add -A
      if git diff --cached --quiet; then
          echo "    (no new changes to commit)"
      else
          git commit -m "pre-maintenance snapshot $(date +%Y-%m-%d-%H%M)"
      fi
    )
    SNAPSHOT_REF="git commit $(cd "$HOME/.openclaw/workspace" && git rev-parse HEAD)"
else
    BACKUP_DIR="$HOME/.openclaw/backups/${REPORT_DATE}-$(date +%H%M)"
    mkdir -p "$BACKUP_DIR"
    cp -R "$HOME/.openclaw/workspace" "$BACKUP_DIR/"
    SNAPSHOT_REF="copy at $BACKUP_DIR"
fi
echo "    snapshot: $SNAPSHOT_REF"

# --- Step 4: brew upgrade node@22 (Rule 3 diff-and-approve) ---
echo
echo ">>> Step 4/8: brew upgrade node@22"
echo
echo "    PROPOSED CHANGE to /opt/homebrew/opt/node@22"
echo "      Current: $NODE_BEFORE"
echo "      After:   the latest Homebrew node@22 build (expected >= 22.22.3)"
echo "    REASON:   openclaw@2026.7.1-2 requires Node >= 22.22.3 on the 22.x line."
echo "    ROLLBACK: Homebrew retains the prior keg in"
echo "              /opt/homebrew/Cellar/node@22/<previous-version>"
echo "              Recover with 'brew switch node@22 <previous>' if available,"
echo "              or manually re-link the old Cellar path."
echo
confirm "    Run 'brew upgrade node@22' now?"
brew upgrade node@22

# --- Step 5: verify the upgrade landed ---
echo
echo ">>> Step 5/8: verify upgraded node@22 version"
if ! NODE_AFTER="$(/opt/homebrew/opt/node@22/bin/node --version 2>&1)"; then
    echo "    ERROR: /opt/homebrew/opt/node@22/bin/node still fails after brew upgrade:"
    echo "$NODE_AFTER" | sed 's/^/      /' | head -5
    echo
    echo "    brew upgrade completed but node@22 is still broken. This is unexpected."
    echo "    Node 24 fallback: brew unlink node@22 && brew link --overwrite node@24"
    echo "                     openclaw gateway restart"
    exit 1
fi
echo "    before: $NODE_BEFORE"
echo "    after:  $NODE_AFTER"
echo
confirm "    Is the after-version >= 22.22.3?"

# --- Step 6: restart the gateway ---
echo
echo ">>> Step 6/8: restart OpenClaw gateway"
confirm "    Run 'openclaw gateway restart' now?"
openclaw gateway restart
sleep 3

# --- Step 7: confirm the gateway responds ---
echo
echo ">>> Step 7/8: confirm gateway responds to --version"
if OPENCLAW_VERSION="$(openclaw --version 2>&1)"; then
    echo "    openclaw --version: $OPENCLAW_VERSION"
else
    echo "    openclaw --version FAILED:"
    echo "$OPENCLAW_VERSION"
    echo
    echo "    Node 24 fallback if gateway will not come back up:"
    echo "      brew unlink node@22 && brew link --overwrite node@24"
    echo "      openclaw gateway restart"
    exit 1
fi

# --- Step 8: retry openclaw update ---
echo
echo ">>> Step 8/8: retry 'openclaw update'"
confirm "    Run 'openclaw update' now?"
UPDATE_EXIT=0
UPDATE_OUTPUT="$(openclaw update 2>&1)" || UPDATE_EXIT=$?
echo "$UPDATE_OUTPUT"
echo
echo "    openclaw update exit code: $UPDATE_EXIT"

# --- Post-op verification (Rule 5) ---
echo
echo "==============================================================="
echo "  Post-op verification (Rule 5)"
echo "==============================================================="

echo
echo "Check 1/4: gateway started clean"
confirm "    Did 'openclaw gateway restart' complete with no error output?"
CHECK_1="PASS"

echo
echo "Check 2/4: agent responds to a test message"
echo "    Send your agent a test message NOW (Telegram, or however you talk to it)."
echo "    Wait for a response before answering."
confirm "    Did your agent respond?"
CHECK_2="PASS"

echo
echo "Check 3/4: identity intact (the actual bar)"
echo "    Ask your agent who it is. It must answer from its SOUL.md,"
echo "    not with generic AI-assistant boilerplate. 'Responds' is not enough."
confirm "    Did your agent answer with ITS identity?"
CHECK_3="PASS"

echo
echo "Check 4/4: workspace path in openclaw.json"
WORKSPACE_LINE="$(grep workspace "$HOME/.openclaw/openclaw.json" 2>/dev/null || echo 'NOT FOUND')"
echo "    $WORKSPACE_LINE"
if [[ "$WORKSPACE_LINE" == *".openclaw/workspace"* ]]; then
    CHECK_4="PASS"
    echo "    PASS"
else
    CHECK_4="FAIL"
    echo "    FAIL: does not contain .openclaw/workspace"
fi

# --- Maintenance report (Rule 6) ---
cat > "$REPORT_PATH" <<REPORT_EOF
# Maintenance report ${REPORT_DATE}

## Task
Recover from Incident 2026-07 followup: \`openclaw update\` was blocked
by a Node runtime version mismatch on the managed gateway service.

## Snapshot (Rule 1)
${SNAPSHOT_REF}

## Package versions
- node@22 before: ${NODE_BEFORE}
- node@22 after:  ${NODE_AFTER}
- openclaw --version at end: ${OPENCLAW_VERSION}
- openclaw update exit code: ${UPDATE_EXIT}

## Commands executed
1. \`brew info node@22 | head -3\` (pre-check)
2. \`/opt/homebrew/opt/node@22/bin/node --version\` (rollback baseline)
3. Workspace snapshot: ${SNAPSHOT_REF}
4. \`brew upgrade node@22\`
5. \`/opt/homebrew/opt/node@22/bin/node --version\` (verify)
6. \`openclaw gateway restart\`
7. \`openclaw --version\`
8. \`openclaw update\`

## openclaw update output
\`\`\`
${UPDATE_OUTPUT}
\`\`\`

## Post-op verification (Rule 5)
1. Gateway started clean:     ${CHECK_1}
2. Agent responds to test:    ${CHECK_2}
3. Identity intact (SOUL.md): ${CHECK_3}
4. Workspace path correct:    ${CHECK_4}

## Followups (Rule 7: recommendations, NOT touched this session)
- Doctor warning: 'Left plugin install index in place because shared SQLite state has
  conflicting plugin install metadata for: brave, codex, discord.' Doctor is refusing
  to auto-resolve a plugin metadata conflict, which is the safe default. Recommend
  running \`openclaw doctor --fix\` INTERACTIVELY (not \`--non-interactive\`) after this
  session so you can see what doctor wants to do before it does it.
- Doctor warning: 'Skipped Memory Core legacy memory index import for agent main
  because legacy rows could not be imported. Error: legacy memory meta rows conflict
  with canonical memory index rows.' A memory-schema migration with row-level
  conflicts. Doctor is skipping the import. Recommend checking whether the skipped
  rows contain any agent memory that matters (sqlite3 on the OpenClaw memory DB,
  or ask your agent about a memory you know it had).

## Anything else noticed
(add here manually if there's anything else worth flagging)
REPORT_EOF

# Commit the report to the workspace repo if it's a git repo
if [ -d "$HOME/.openclaw/workspace/.git" ]; then
    ( cd "$HOME/.openclaw/workspace"
      git add reports/
      git commit -m "maintenance report ${REPORT_DATE}" 2>/dev/null || echo "  (report already committed or nothing new)"
    )
fi

# --- Summary ---
echo
echo "==============================================================="
echo "  Done."
echo "==============================================================="
echo "  node@22:              $NODE_BEFORE  ->  $NODE_AFTER"
echo "  openclaw --version:   $OPENCLAW_VERSION"
echo "  openclaw update exit: $UPDATE_EXIT"
echo "  gateway clean start:  $CHECK_1"
echo "  agent responds:       $CHECK_2"
echo "  identity intact:      $CHECK_3"
echo "  workspace path ok:    $CHECK_4"
echo
echo "  Report: $REPORT_PATH"

if [[ "$CHECK_1" == "PASS" && "$CHECK_2" == "PASS" && "$CHECK_3" == "PASS" && "$CHECK_4" == "PASS" && "$UPDATE_EXIT" == "0" ]]; then
    echo "  STATUS: SUCCESS"
    exit 0
else
    echo "  STATUS: PARTIAL (see report and Rule 5 above)"
    exit 1
fi
