#!/usr/bin/env bash
# Deploys the demo EVPN-VXLAN fabric and starts the fcli web UI.
#
# Runs in the background so opening the Codespace is never blocked by the slow
# parts (SR Linux image pulls and fabric convergence). Everything it does is
# narrated into $FCLI_SETUP_LOG; `.devcontainer/status.sh` summarises it.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=lib.sh
source "${REPO_ROOT}/.devcontainer/lib.sh"

exec >>"${FCLI_SETUP_LOG}" 2>&1

if [ -f "${FCLI_SETUP_LOCK}" ]; then
    running_pid="$(cat "${FCLI_SETUP_LOCK}" 2>/dev/null || true)"
    if [ -n "${running_pid}" ] && kill -0 "${running_pid}" 2>/dev/null; then
        say "setup already running (pid ${running_pid}), nothing to do"
        exit 0
    fi
fi
echo $$ >"${FCLI_SETUP_LOCK}"
trap 'rm -f "${FCLI_SETUP_LOCK}"' EXIT

STARTED=${SECONDS}
elapsed() { printf '%dm%02ds' $(( (SECONDS - STARTED) / 60 )) $(( (SECONDS - STARTED) % 60 )); }
srl_running() { docker ps --filter 'ancestor=ghcr.io/nokia/srlinux:24.10.2' --format '{{.Names}}' 2>/dev/null | wc -l; }

say ""
say "================================================================"
say " fcli Codespace setup - $(date -Is)"
say ""
say " 1. wait for the Docker daemon"
say " 2. install fcli from the sources in this repo"
say " 3. deploy the demo fabric (8 SR Linux nodes + 9 servers)"
say " 4. wait until every node answers gNMI"
say " 5. start the fcli web UI on port 8080"
say ""
say " Step 3 is the slow one: each SR Linux image is about 1 GB, so"
say " the first run usually takes 15-30 minutes."
say "================================================================"

step "[1/5] Waiting for the Docker daemon"
docker_ready=false
for _ in $(seq 1 90); do
    if docker info >/dev/null 2>&1; then
        docker_ready=true
        break
    fi
    sleep 2
done
if [ "${docker_ready}" != true ]; then
    say "ERROR: the Docker daemon did not come up within 3 minutes." >&2
    say "       Docker-in-Docker may have failed to start; rebuild the container." >&2
    exit 1
fi
detail "Docker is up ($(elapsed))"

step "[2/5] Installing fcli from local sources"
ensure_uv || exit 1
export PATH="${REPO_ROOT}/.venv/bin:${HOME}/.local/bin:${PATH}"
if ! uv sync --extra dev; then
    say "ERROR: 'uv sync' failed - fcli could not be installed." >&2
    exit 1
fi
detail "fcli installed at $(command -v fcli) ($(elapsed))"

step "[3/5] Deploying the demo fabric from ${FCLI_TOPO}"
detail "pulling images and booting 17 containers, this is the long wait"
if ! containerlab deploy -t "${FCLI_TOPO}"; then
    say "ERROR: 'containerlab deploy' failed." >&2
    say "       Inspect with: containerlab inspect -t ${FCLI_TOPO}" >&2
    docker ps -a || true
    exit 1
fi
detail "$(srl_running) SR Linux nodes running ($(elapsed))"

step "[4/5] Waiting for gNMI on every node"
detail "nodes accept gNMI only once SR Linux has finished booting"
gnmi_deadline=$(( SECONDS + 1800 ))
until fcli -t "${FCLI_TOPO}" sys-info >/dev/null 2>&1; do
    if (( SECONDS >= gnmi_deadline )); then
        say "ERROR: no gNMI answer after 30 minutes." >&2
        say "       Check the nodes with: containerlab inspect -t ${FCLI_TOPO}" >&2
        containerlab inspect -t "${FCLI_TOPO}" 2>/dev/null || true
        exit 1
    fi
    detail "still booting, $(srl_running) nodes up ($(elapsed))"
    sleep 20
done
detail "all nodes answer gNMI ($(elapsed))"

step "[5/5] Starting the fcli web UI on port 8080"
if [ -f "${FCLI_SERVER_PID}" ] && kill -0 "$(cat "${FCLI_SERVER_PID}" 2>/dev/null)" 2>/dev/null; then
    detail "already running (pid $(cat "${FCLI_SERVER_PID}"))"
else
    nohup fcli -t "${FCLI_TOPO}" server --listen 0.0.0.0 --port 8080 >"${FCLI_SERVER_LOG}" 2>&1 &
    echo $! >"${FCLI_SERVER_PID}"
    sleep 3
    if ! kill -0 "$(cat "${FCLI_SERVER_PID}")" 2>/dev/null; then
        say "ERROR: the fcli server exited immediately. Last lines of ${FCLI_SERVER_LOG}:" >&2
        tail -20 "${FCLI_SERVER_LOG}" 2>/dev/null || true
        exit 1
    fi
    detail "server running (pid $(cat "${FCLI_SERVER_PID}"))"
fi

step "Setup complete in $(elapsed)"
say ""
say " Open port 8080 from the Ports tab for the live web UI."
say " CLI against the same fabric:  fcli -t ${FCLI_TOPO} bgp-peers"
say ""
