#!/usr/bin/env bash
# Idempotent launcher for background lab deploy + fcli server.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

LOGFILE="/tmp/fcli-codespace-setup.log"
POSTSTART_LOG="/tmp/fcli-post-start.log"
LOCKFILE="/tmp/fcli-codespace-setup.lock"
SETUP="${REPO_ROOT}/.devcontainer/setup.sh"

{
    echo "=== $(date -Is) launch-setup (pid $$, cwd ${PWD}) ==="
} >>"${POSTSTART_LOG}"

if [ -f "${LOCKFILE}" ]; then
    old_pid="$(cat "${LOCKFILE}")"
    if kill -0 "${old_pid}" 2>/dev/null; then
        echo "setup already running (pid ${old_pid})" | tee -a "${POSTSTART_LOG}"
        echo "   tail -f ${LOGFILE}"
        exit 0
    fi
fi

if [ ! -f "${LOGFILE}" ] || [ ! -s "${LOGFILE}" ]; then
    : >"${LOGFILE}"
fi

echo "== fcli codespace setup starting in the background" | tee -a "${POSTSTART_LOG}"
echo "   tail -f ${LOGFILE}   # watch progress" | tee -a "${POSTSTART_LOG}"

nohup bash "${SETUP}" >>"${LOGFILE}" 2>&1 &
disown || true
