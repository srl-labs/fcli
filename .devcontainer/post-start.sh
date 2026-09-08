#!/usr/bin/env bash
set -euo pipefail

TOPO="labs/demo/demo.clab.yaml"
PIDFILE="/tmp/fcli-server.pid"
LOGFILE="/tmp/fcli-server.log"
export PATH="${HOME}/.local/bin:${PATH}:${PWD}/.venv/bin"

if ! command -v fcli >/dev/null 2>&1; then
    uv sync --extra dev
fi

echo "== Ensuring demo lab is deployed"
containerlab deploy -t "${TOPO}"

if [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "== fcli server already running (pid $(cat "${PIDFILE}"))"
else
    echo "== Starting fcli server on http://0.0.0.0:8080"
    nohup fcli -t "${TOPO}" server --listen 0.0.0.0 --port 8080 >"${LOGFILE}" 2>&1 &
    echo $! >"${PIDFILE}"
fi

echo "== Open port 8080 in the Ports tab for the live web UI"
