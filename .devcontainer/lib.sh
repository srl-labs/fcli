#!/usr/bin/env bash
# Shared helpers for the Codespaces lifecycle scripts.

FCLI_SETUP_LOG="${FCLI_SETUP_LOG:-/tmp/fcli-codespace-setup.log}"
FCLI_SETUP_STATUS="${FCLI_SETUP_STATUS:-/tmp/fcli-setup-status}"
FCLI_SERVER_LOG="${FCLI_SERVER_LOG:-/tmp/fcli-server.log}"
FCLI_SERVER_PID="${FCLI_SERVER_PID:-/tmp/fcli-server.pid}"
FCLI_SETUP_LOCK="${FCLI_SETUP_LOCK:-/tmp/fcli-codespace-setup.lock}"
FCLI_TOPO="${FCLI_TOPO:-labs/demo/demo.clab.yaml}"

say() { printf '%s\n' "$*"; }
detail() { printf '        %s\n' "$*"; }

# A numbered step: headline in the log, and one line in the status file so
# `status.sh` can answer "where is it now" without parsing the whole log.
step() {
    printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"
    printf '%s\n' "$*" >"${FCLI_SETUP_STATUS}"
}

# The containerlab devcontainer image ships atuin, whose sqlite pool times out
# on the Codespaces disk and prints a Rust backtrace after every prompt. Atuin
# has no env var to skip its init, so the shell rc line is what has to go.
silence_atuin() {
    local rc
    local touched=false
    for rc in "${HOME}/.zshrc" "${HOME}/.bashrc"; do
        [ -f "${rc}" ] || continue
        if grep -q '^[[:space:]]*eval "\$(atuin init' "${rc}"; then
            sed -i 's|^\([[:space:]]*eval "\$(atuin init.*\)$|# \1  # disabled by .devcontainer/lib.sh|' "${rc}"
            detail "atuin disabled in ${rc}"
            touched=true
        fi
    done
    if [ "${touched}" = true ]; then
        detail "open a new terminal to lose the atuin errors in this session"
    else
        detail "atuin already disabled"
    fi
}

ensure_uv() {
    export PATH="${HOME}/.local/bin:${PATH}"
    if command -v uv >/dev/null 2>&1; then
        detail "uv found at $(command -v uv)"
        return 0
    fi
    detail "uv not on PATH, installing it"
    sudo apt-get update -qq
    sudo apt-get install --no-install-recommends -y curl ca-certificates
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
    if ! command -v uv >/dev/null 2>&1; then
        say "ERROR: uv install failed" >&2
        return 1
    fi
    detail "uv installed at $(command -v uv)"
}
