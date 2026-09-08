#!/usr/bin/env bash
# Starts setup.sh in the background, at most once at a time.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=lib.sh
source "${REPO_ROOT}/.devcontainer/lib.sh"

if [ -f "${FCLI_SETUP_LOCK}" ]; then
    running_pid="$(cat "${FCLI_SETUP_LOCK}" 2>/dev/null || true)"
    if [ -n "${running_pid}" ] && kill -0 "${running_pid}" 2>/dev/null; then
        say "Setup is already running (pid ${running_pid})."
        say "  tail -f ${FCLI_SETUP_LOG}"
        exit 0
    fi
fi

say "Deploying the demo fabric and starting the fcli web UI in the background."
say "  tail -f ${FCLI_SETUP_LOG}          # watch it work"
say "  bash .devcontainer/status.sh       # short summary"
say "First run takes 15-30 minutes; the Codespace is usable meanwhile."

nohup bash "${REPO_ROOT}/.devcontainer/setup.sh" >/dev/null 2>&1 &
disown 2>/dev/null || true
