#!/usr/bin/env bash
# tekRESCUE Mechanic for OpenClaw - installer
#
# Writes the two LaunchAgent plists, copies .env.example into the user's
# config directory (if .env doesn't exist yet), loads the agents, and
# offers to capture first-known-good if OpenClaw is currently healthy.
#
# Usage:
#   ./install.sh                          interactive, prompts for backup confirmation
#   ./install.sh --i-have-a-backup        non-interactive, skips the backup prompt
#   ./install.sh --skip-test-fire         skip the post-install supervisor + updater
#                                         test-fire (for scripted / CI installs)

set -euo pipefail

SKIP_WARNING=0
SKIP_TEST_FIRE=0
for arg in "$@"; do
    case "$arg" in
        --i-have-a-backup) SKIP_WARNING=1 ;;
        --skip-test-fire) SKIP_TEST_FIRE=1 ;;
        -h|--help)
            sed -n '2,14p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" 1>&2
            exit 2
            ;;
    esac
done

print_warning() {
    cat <<'WARN'
================================================================
  tekRESCUE Mechanic for OpenClaw - before you install
================================================================

Mechanic schedules nightly maintenance against OpenClaw and/or
Hermes Agent (whichever TARGETS lists). It updates them a week
behind the newest release, runs their doctors while you sleep,
verifies the result, and tells you exactly how to roll back if
something looks wrong.

The blast radius is small but real. Back up OpenClaw's config
directory (and run `hermes backup` if you use Hermes) before you
run this installer the first time. Mechanic will not take that
first backup for you.

This software ships with NO WARRANTY. We are not responsible if
Godzilla tears down your server farm or if this script deletes
everything on your machine. Use responsibly.

================================================================
WARN
}

confirm_backup() {
    if [ "$SKIP_WARNING" = "1" ]; then
        echo "Skipping backup confirmation (--i-have-a-backup)."
        return
    fi
    print_warning
    echo
    read -r -p "Have you backed up your agent's data (OpenClaw config dir, hermes backup)? [y/N] " answer
    case "${answer:-N}" in
        y|Y|yes|YES)
            echo "Backup confirmed. Proceeding."
            ;;
        *)
            echo "Aborting. Back up first, then re-run this installer."
            exit 1
            ;;
    esac
}

require_binary() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Required binary not found: $1" 1>&2
        echo "Install it and re-run install.sh." 1>&2
        exit 1
    fi
}

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${HOME}"
CONFIG_DIR="${HOME_DIR}/.config/tekrescue-mechanic"
CONFIG_FILE="${CONFIG_DIR}/.env"
LAUNCHAGENTS_DIR="${HOME_DIR}/Library/LaunchAgents"
LOG_DIR="${HOME_DIR}/Library/Logs/tekrescue-mechanic"
STATE_DIR="${HOME_DIR}/Library/Application Support/tekrescue-mechanic"
SNAPSHOT_DIR="${STATE_DIR}/snapshots"
RUNTIME_STATE_DIR="${STATE_DIR}/state"

SUPERVISOR_LABEL="com.tekrescue.mechanic.supervisor"
UPDATER_LABEL="com.tekrescue.mechanic.updater"
SUPERVISOR_PLIST="${LAUNCHAGENTS_DIR}/${SUPERVISOR_LABEL}.plist"
UPDATER_PLIST="${LAUNCHAGENTS_DIR}/${UPDATER_LABEL}.plist"

confirm_backup

require_binary launchctl
require_binary mechanic
require_binary mechanic-updater
require_binary mechanic-supervisor

MECHANIC_UPDATER_BIN="$(command -v mechanic-updater)"
MECHANIC_SUPERVISOR_BIN="$(command -v mechanic-supervisor)"

echo
echo "Resolved entry points:"
echo "  updater:    ${MECHANIC_UPDATER_BIN}"
echo "  supervisor: ${MECHANIC_SUPERVISOR_BIN}"
echo

mkdir -p "${CONFIG_DIR}" "${LAUNCHAGENTS_DIR}" "${LOG_DIR}" "${SNAPSHOT_DIR}" "${RUNTIME_STATE_DIR}"

if [ ! -f "${CONFIG_FILE}" ]; then
    cp "${REPO_DIR}/.env.example" "${CONFIG_FILE}"
    chmod 600 "${CONFIG_FILE}"
    echo "Created ${CONFIG_FILE} from .env.example."
    echo "Edit it now to point OPENCLAW_BIN_PATH and OPENCLAW_CONFIG_PATH at your install."
else
    chmod 600 "${CONFIG_FILE}"
    echo "Found existing ${CONFIG_FILE}; leaving it alone."
fi

# Pull UPDATE_TIME and SUPERVISOR_INTERVAL_MINUTES out of the .env if present.
UPDATE_TIME="$(grep -E '^UPDATE_TIME=' "${CONFIG_FILE}" | tail -1 | cut -d= -f2 || true)"
UPDATE_TIME="${UPDATE_TIME:-02:00}"
UPDATE_HOUR="${UPDATE_TIME%%:*}"
UPDATE_MINUTE="${UPDATE_TIME##*:}"
SUPERVISOR_MINUTES="$(grep -E '^SUPERVISOR_INTERVAL_MINUTES=' "${CONFIG_FILE}" | tail -1 | cut -d= -f2 || true)"
SUPERVISOR_MINUTES="${SUPERVISOR_MINUTES:-240}"
SUPERVISOR_SECONDS=$(( SUPERVISOR_MINUTES * 60 ))

echo
echo "Writing LaunchAgents..."

cat > "${UPDATER_PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${UPDATER_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${MECHANIC_UPDATER_BIN}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${UPDATE_HOUR}</integer>
        <key>Minute</key>
        <integer>${UPDATE_MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/updater.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/updater.stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST

cat > "${SUPERVISOR_PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SUPERVISOR_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${MECHANIC_SUPERVISOR_BIN}</string>
    </array>
    <key>StartInterval</key>
    <integer>${SUPERVISOR_SECONDS}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/supervisor.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/supervisor.stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST

chmod 644 "${UPDATER_PLIST}" "${SUPERVISOR_PLIST}"
echo "  wrote ${UPDATER_PLIST}"
echo "  wrote ${SUPERVISOR_PLIST}"

# Reload: unload any prior version first, ignore errors, then load fresh.
launchctl unload "${UPDATER_PLIST}" 2>/dev/null || true
launchctl unload "${SUPERVISOR_PLIST}" 2>/dev/null || true
launchctl load "${UPDATER_PLIST}"
launchctl load "${SUPERVISOR_PLIST}"

echo
echo "LaunchAgents loaded. Verifying with launchctl list..."
if launchctl list "${UPDATER_LABEL}" >/dev/null 2>&1; then
    echo "  ${UPDATER_LABEL}: loaded"
else
    echo "  ${UPDATER_LABEL}: NOT loaded; check the log directory and re-run install.sh" 1>&2
    exit 1
fi
if launchctl list "${SUPERVISOR_LABEL}" >/dev/null 2>&1; then
    echo "  ${SUPERVISOR_LABEL}: loaded"
else
    echo "  ${SUPERVISOR_LABEL}: NOT loaded; check the log directory and re-run install.sh" 1>&2
    exit 1
fi

# Foreground test-fire so any first-run macOS permission dialog appears
# while the operator is still at the terminal, not at 02:00 when the
# updater fires unattended and the prompt lands in Notification Center.
OPENCLAW_BIN_VAL="$(grep -E '^OPENCLAW_BIN_PATH=' "${CONFIG_FILE}" | tail -1 | cut -d= -f2- || true)"
OPENCLAW_CONFIG_VAL="$(grep -E '^OPENCLAW_CONFIG_PATH=' "${CONFIG_FILE}" | tail -1 | cut -d= -f2- || true)"
TARGETS_VAL="$(grep -E '^TARGETS=' "${CONFIG_FILE}" | tail -1 | cut -d= -f2- || true)"
TARGETS_VAL="${TARGETS_VAL:-openclaw}"
NEEDS_OPENCLAW=0
case ",${TARGETS_VAL// /}," in *,openclaw,*) NEEDS_OPENCLAW=1 ;; esac

echo
if [ "$SKIP_TEST_FIRE" = "1" ]; then
    echo "Skipping test-fire (--skip-test-fire). The LaunchAgents will fire on"
    echo "their schedule. If a macOS permission dialog appears while you are"
    echo "away from the Mac and you click 'Don't Allow' from habit, Mechanic"
    echo "will silently break; re-grant via System Settings -> Privacy & Security."
elif [ "$NEEDS_OPENCLAW" = "1" ] && { [ -z "$OPENCLAW_BIN_VAL" ] || [ -z "$OPENCLAW_CONFIG_VAL" ]; }; then
    echo "Skipping test-fire: openclaw is in TARGETS but OPENCLAW_BIN_PATH or"
    echo "OPENCLAW_CONFIG_PATH is still empty in:"
    echo "  ${CONFIG_FILE}"
    echo
    echo "After you edit those values, run:"
    echo "    mechanic-supervisor"
    echo "    mechanic-updater --test-fire"
    echo
    echo "once from this terminal to trigger any first-run macOS permission"
    echo "prompts in context. If a prompt fires while you are not at the Mac,"
    echo "it can land in Notification Center and a habitual 'Don't Allow'"
    echo "click will silently break Mechanic."
else
    echo "================================================================"
    echo "  Foreground test-fire"
    echo "================================================================"
    echo
    echo "macOS may prompt you to grant Full Disk Access or other"
    echo "permissions the first time Python is run from a LaunchAgent."
    echo "Running supervisor and updater foreground now so any prompt"
    echo "appears while you are still here."
    echo
    echo "Total time: ~6 to 10 minutes (the updater test-fire snapshots"
    echo "each target's data and runs its doctor, but skips the actual"
    echo "update step)."
    echo

    echo "1/2: mechanic-supervisor (heartbeat + update check, ~5 sec)..."
    if mechanic-supervisor; then
        echo "  supervisor test-fire passed"
    else
        echo "  supervisor test-fire FAILED (exit non-zero)"
        echo "  log: ${LOG_DIR}/mechanic.log"
        echo "  the LaunchAgents are still installed and will retry on schedule"
    fi

    echo
    echo "2/2: mechanic-updater --test-fire (snapshot + doctor + verify, ~6 min)..."
    if mechanic-updater --test-fire; then
        echo
        echo "  updater test-fire passed"
    else
        echo
        echo "  updater test-fire FAILED (exit non-zero)"
        echo "  log: ${LOG_DIR}/mechanic.log"
        echo "  the LaunchAgents are still installed and will retry on schedule"
    fi

    echo
    echo "If you saw a macOS permission dialog during either run, the answer"
    echo "is almost always 'Allow' or 'Open Anyway' (Gatekeeper)."
    echo "If you accidentally clicked 'Don't Allow', re-grant via"
    echo "System Settings -> Privacy & Security."
fi

echo
echo "Install complete."
echo
echo "Next steps:"
echo "  1. Edit ${CONFIG_FILE}: set TARGETS (openclaw, hermes, or both) and the paths for each."
echo "  2. Run \`mechanic status\` to confirm Mechanic sees your targets, then \`mechanic plan\`"
echo "     to see what the first nightly would do."
echo "  3. Run \`mechanic-supervisor\` once and then \`mechanic-updater --test-fire\` once."
echo "     These surface any first-run macOS permission prompts while you are at the keyboard."
echo "     The updater test-fire takes about 6 minutes (snapshots OpenClaw, runs doctor, verifies,"
echo "     but does NOT run \`openclaw update\`)."
echo "  4. Run \`mechanic capture-first-good\` while OpenClaw is healthy to anchor the rollback baseline."
echo "  5. (Optional, v0.2 prep) \`mechanic capture-prompts\` records doctor prompt/response pairs."
echo "     v0.1.1 runs doctor with --non-interactive, so this is dormant for now."
echo
echo "macOS may prompt you to grant Full Disk Access or Automation permissions"
echo "for Python the first time the supervisor or updater fires. Click Allow."
echo "See the README's Troubleshooting section if the prompt is missed or denied."
