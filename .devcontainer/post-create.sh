#!/usr/bin/env bash
set -euo pipefail

TOPO="labs/demo/demo.clab.yaml"
export PATH="${PWD}/.venv/bin:${HOME}/.local/bin:${PATH}"

echo "== Installing fcli from local sources"
uv sync --extra dev

echo "== Deploying demo lab"
containerlab deploy -t "${TOPO}"

echo "== Waiting for gNMI (up to 20 minutes)"
deadline=$((SECONDS + 1200))
until fcli -t "${TOPO}" sys-info >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        echo "Timed out waiting for gNMI on the demo lab" >&2
        containerlab inspect -t "${TOPO}" || true
        exit 1
    fi
    echo "  still waiting..."
    sleep 15
done

echo "== Demo lab is ready"
