#!/usr/bin/env bash
# Shared helpers for Codespaces lifecycle scripts.

ensure_uv() {
    export PATH="${HOME}/.local/bin:${PATH}"
    if command -v uv >/dev/null 2>&1; then
        echo "== uv: $(command -v uv)"
        return 0
    fi
    echo "== Installing uv"
    sudo apt-get update -qq
    sudo apt-get install --no-install-recommends -y curl ca-certificates
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
    if ! command -v uv >/dev/null 2>&1; then
        echo "ERROR: uv install failed" >&2
        return 1
    fi
    echo "== uv installed: $(command -v uv)"
}
