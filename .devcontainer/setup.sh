#!/usr/bin/env bash
# Full lab deploy + fcli server startup.  Runs in the background so Codespaces
# creation is not blocked by image pulls and fabric convergence.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

TOPO="labs/demo/demo.clab.yaml"
LOGFILE="/tmp/fcli-codespace-setup.log"
LOCKFILE="/tmp/fcli-codespace-setup.lock"
PIDFILE="/tmp/fcli-server.pid"
SERVER_LOG="/tmp/fcli-server.log"

exec >>"${LOGFILE}" 2>&1
echo ""
echo "=== $(date -Is) fcli codespace setup starting (pid $$) ==="

if [ -f "${LOCKFILE}" ]; then
    old_pid="$(cat "${LOCKFILE}")"
    if kill -0 "${old_pid}" 2>/dev/null; then
        echo "setup already running (pid ${old_pid})"
        exit 0
    fi
fi
echo $$ >"${LOCKFILE}"

# shellcheck source=lib.sh
source "${REPO_ROOT}/.devcontainer/lib.sh"
ensure_uv || exit 1

export PATH="${REPO_ROOT}/.venv/bin:${HOME}/.local/bin:${PATH}"

wait_for_docker() {
    echo "== Waiting for Docker daemon"
    local i
    for i in $(seq 1 90); do
        if docker info >/dev/null 2>&1; then
            echo "   Docker is ready"
            return 0
        fi
        sleep 2
    done
    echo "ERROR: Docker daemon not ready after 3 minutes" >&2
    return 1
}

install_fcli() {
    if command -v fcli >/dev/null 2>&1; then
        echo "== fcli already installed: $(command -v fcli)"
        return 0
    fi
    echo "== Installing fcli"
    uv sync --extra dev
}

deploy_lab() {
    echo "== Deploying demo lab"
    if ! containerlab deploy -t "${TOPO}"; then
        echo "ERROR: containerlab deploy failed" >&2
        containerlab inspect -t "${TOPO}" 2>/dev/null || true
        return 1
    fi
    containerlab inspect -t "${TOPO}" || true
}

wait_for_gnmi() {
    echo "== Waiting for gNMI (up to 30 minutes)"
    local deadline=$((SECONDS + 1800))
    until fcli -t "${TOPO}" sys-info >/dev/null 2>&1; do
        if (( SECONDS >= deadline )); then
            echo "ERROR: timed out waiting for gNMI" >&2
            containerlab inspect -t "${TOPO}" 2>/dev/null || true
            return 1
        fi
        echo "   still waiting for gNMI..."
        sleep 20
    done
    echo "== gNMI is up"
}

start_server() {
    if [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
        echo "== fcli server already running (pid $(cat "${PIDFILE}"))"
        return 0
    fi
    echo "== Starting fcli server on http://0.0.0.0:8080"
    nohup fcli -t "${TOPO}" server --listen 0.0.0.0 --port 8080 >"${SERVER_LOG}" 2>&1 &
    echo $! >"${PIDFILE}"
    sleep 2
    if kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
        echo "== fcli server started (pid $(cat "${PIDFILE}"))"
    else
        echo "ERROR: fcli server failed to start; see ${SERVER_LOG}" >&2
        tail -20 "${SERVER_LOG}" 2>/dev/null || true
        return 1
    fi
}

status=0
wait_for_docker || status=1
if [ "${status}" -eq 0 ]; then
    install_fcli || status=1
fi
if [ "${status}" -eq 0 ]; then
    deploy_lab || status=1
fi
if [ "${status}" -eq 0 ]; then
    wait_for_gnmi || status=1
fi
if [ "${status}" -eq 0 ]; then
    start_server || status=1
fi

rm -f "${LOCKFILE}"
if [ "${status}" -eq 0 ]; then
    echo "=== $(date -Is) setup complete — open port 8080 in the Ports tab ==="
else
    echo "=== $(date -Is) setup finished with errors (status ${status}) ==="
fi
exit "${status}"
