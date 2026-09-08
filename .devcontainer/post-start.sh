#!/usr/bin/env bash
# Kick off background lab deploy + fcli server on every container start.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

LOGFILE="/tmp/fcli-codespace-setup.log"
SETUP="${REPO_ROOT}/.devcontainer/setup.sh"

: >"${LOGFILE}"
echo "== fcli codespace setup started in the background"
echo "   tail -f ${LOGFILE}   # watch progress"

nohup bash "${SETUP}" >/dev/null 2>&1 &
