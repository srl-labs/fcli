#!/usr/bin/env bash
# Short answer to "is the demo fabric up yet?"
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=lib.sh
source "${REPO_ROOT}/.devcontainer/lib.sh"

if [ -f "${FCLI_SETUP_LOCK}" ] && kill -0 "$(cat "${FCLI_SETUP_LOCK}" 2>/dev/null)" 2>/dev/null; then
    say "setup:  running (pid $(cat "${FCLI_SETUP_LOCK}"))"
else
    say "setup:  not running"
fi

if [ -f "${FCLI_SETUP_STATUS}" ]; then
    say "step:   $(cat "${FCLI_SETUP_STATUS}")"
fi

say "nodes:  $(docker ps --format '{{.Names}}' 2>/dev/null | grep -c . || echo 0) container(s) running"

if [ -f "${FCLI_SERVER_PID}" ] && kill -0 "$(cat "${FCLI_SERVER_PID}" 2>/dev/null)" 2>/dev/null; then
    say "server: up on port 8080 (pid $(cat "${FCLI_SERVER_PID}"))"
else
    say "server: down"
fi

if [ -f "${FCLI_SETUP_LOG}" ]; then
    say ""
    say "last lines of ${FCLI_SETUP_LOG}:"
    tail -15 "${FCLI_SETUP_LOG}"
fi
