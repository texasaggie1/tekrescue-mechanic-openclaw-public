#!/usr/bin/env bash
# tekRESCUE Mechanic for OpenClaw - uninstaller
#
# Unloads the two LaunchAgents, removes the plists, removes the user's
# config directory. With --purge, also removes logs, snapshots, and
# runtime state.
#
# uninstall.sh does NOT roll OpenClaw back to first-known-good. If you
# want that, run `mechanic restore first-known-good` separately before
# uninstalling.
#
# Usage:
#   ./uninstall.sh           remove LaunchAgents and config
#   ./uninstall.sh --purge   also wipe logs, snapshots, runtime state

set -euo pipefail

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" 1>&2
            exit 2
            ;;
    esac
done

HOME_DIR="${HOME}"
CONFIG_DIR="${HOME_DIR}/.config/tekrescue-mechanic"
LAUNCHAGENTS_DIR="${HOME_DIR}/Library/LaunchAgents"
LOG_DIR="${HOME_DIR}/Library/Logs/tekrescue-mechanic"
STATE_DIR="${HOME_DIR}/Library/Application Support/tekrescue-mechanic"

SUPERVISOR_PLIST="${LAUNCHAGENTS_DIR}/com.tekrescue.mechanic.supervisor.plist"
UPDATER_PLIST="${LAUNCHAGENTS_DIR}/com.tekrescue.mechanic.updater.plist"

echo "Unloading LaunchAgents..."
launchctl unload "${UPDATER_PLIST}" 2>/dev/null || true
launchctl unload "${SUPERVISOR_PLIST}" 2>/dev/null || true

echo "Removing LaunchAgent plists..."
rm -f "${UPDATER_PLIST}" "${SUPERVISOR_PLIST}"

if [ -d "${CONFIG_DIR}" ]; then
    echo "Removing ${CONFIG_DIR}..."
    rm -rf "${CONFIG_DIR}"
fi

if [ "$PURGE" = "1" ]; then
    echo "--purge: removing logs and snapshots..."
    if [ -d "${LOG_DIR}" ]; then
        rm -rf "${LOG_DIR}"
        echo "  removed ${LOG_DIR}"
    fi
    if [ -d "${STATE_DIR}" ]; then
        rm -rf "${STATE_DIR}"
        echo "  removed ${STATE_DIR}"
    fi
else
    echo
    echo "Kept (not purged):"
    [ -d "${LOG_DIR}" ] && echo "  ${LOG_DIR}"
    [ -d "${STATE_DIR}" ] && echo "  ${STATE_DIR}"
    echo
    echo "Pass --purge to delete them too."
fi

echo
echo "Uninstall complete. OpenClaw itself was not touched."
echo "If you want to roll OpenClaw back to its pre-Mechanic state,"
echo "run \`mechanic restore first-known-good\` BEFORE re-installing."
