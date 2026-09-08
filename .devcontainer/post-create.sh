#!/usr/bin/env bash
# Fast post-create: install fcli, then kick off background lab deploy.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=lib.sh
source "${REPO_ROOT}/.devcontainer/lib.sh"
ensure_uv

export PATH="${REPO_ROOT}/.venv/bin:${HOME}/.local/bin:${PATH}"

echo "== Installing fcli from local sources"
uv sync --extra dev

echo "== fcli installed; starting lab deploy"
bash "${REPO_ROOT}/.devcontainer/launch-setup.sh"
