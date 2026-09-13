#!/usr/bin/env bash
# Runs once when the Codespace is created. Kept fast: the lab deploy is slow,
# so it is handed to setup.sh in the background rather than blocking creation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=lib.sh
source "${REPO_ROOT}/.devcontainer/lib.sh"

say "== Tidying up the shell"
silence_atuin

say "== Handing off to the background setup"
bash "${REPO_ROOT}/.devcontainer/launch-setup.sh"
