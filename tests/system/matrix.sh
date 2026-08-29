#!/usr/bin/env bash
# Record the report fixtures for one SR Linux release after another.
#
# The fabric is configured once, on the oldest release, by the intent playbook
# from srl-labs/intent-based-ansible-lab. Every later release then inherits that
# same configuration through SR Linux's own config transformation: the configs
# are saved, the image is swapped, and the lab is redeployed *without*
# --reconfigure. That is deliberate - a report has to work against the datamodel
# a real upgrade leaves behind, not against a config regenerated for the new
# release.
#
#   ./matrix.sh all                 # the whole matrix, from scratch
#   ./matrix.sh deploy 25.3.2       # first release: deploy and configure
#   ./matrix.sh upgrade 25.10.3     # save, swap image, redeploy, capture
#
# Requires: containerlab, docker, uv, and a clone of
# https://github.com/srl-labs/intent-based-ansible-lab
set -euo pipefail

RELEASES=(25.3.2 25.10.3 26.3.1 26.7.1)

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="${LAB_DIR:-${HOME}/srl-matrix-lab}"
ANSIBLE_LAB="${ANSIBLE_LAB:-${HOME}/github/intent-based-ansible-lab}"
TOPO="${LAB_DIR}/matrix.clab.yml"
NODES=(l1 l2 l3 l4 s1 s2)
# One leaf and one spine is enough: the leaf carries the services, the LAGs, the
# ethernet segments and the learned state, the spine the underlay-only case
# where most of those reports are legitimately empty.
CAPTURE_NODES=(clab-4l2s-l1=leaf clab-4l2s-s1=spine)

log() { printf '\n== %s\n' "$*"; }

clab() {
  local release="$1"
  shift
  SRL_IMAGE="ghcr.io/nokia/srlinux:${release}" \
    sudo -E containerlab "$@" -t "${TOPO}"
}

# The topology is the lab's own, with the image made settable so one lab
# directory - and so one set of saved configs - carries across releases.
prepare_topology() {
  mkdir -p "${LAB_DIR}"
  if [ ! -f "${TOPO}" ]; then
    sed 's|^      image: ghcr.io/nokia/srlinux:.*|      image: ${SRL_IMAGE}|' \
      "${ANSIBLE_LAB}/topo.clab.yml" >"${TOPO}"
    log "wrote ${TOPO}"
  fi
}

wait_for_nodes() {
  log "waiting for the nodes to boot"
  for _ in $(seq 1 60); do
    local up=0
    for node in "${NODES[@]}"; do
      docker exec "clab-4l2s-${node}" sr_cli 'show version' >/dev/null 2>&1 &&
        up=$((up + 1))
    done
    printf '  %d/%d responding\n' "${up}" "${#NODES[@]}"
    [ "${up}" -eq "${#NODES[@]}" ] && return 0
    sleep 15
  done
  echo "nodes did not come up" >&2
  return 1
}

wait_for_convergence() {
  log "waiting for BGP to converge"
  for _ in $(seq 1 40); do
    local converged=0
    for node in "${NODES[@]}"; do
      # "N configured neighbors, N configured sessions are established"
      if docker exec "clab-4l2s-${node}" sr_cli \
        'show network-instance default protocols bgp neighbor' 2>/dev/null |
        awk '/configured neighbors/ {
               if ($1 > 0 && $1 == $4) ok = 1
             } END { exit ok ? 0 : 1 }'; then
        converged=$((converged + 1))
      fi
    done
    printf '  %d/%d converged\n' "${converged}" "${#NODES[@]}"
    if [ "${converged}" -eq "${#NODES[@]}" ]; then
      # EVPN routes, ethernet segment DF election and LACP settle after the
      # sessions come up, and the state reports are the point of this exercise.
      sleep 30
      return 0
    fi
    sleep 15
  done
  echo "BGP did not converge" >&2
  return 1
}

configure_fabric() {
  log "configuring the fabric with the intent playbook"
  (
    cd "${ANSIBLE_LAB}"
    uv run ansible-playbook -i inv --diff \
      -e "intent_dir=${ANSIBLE_LAB}/intent" playbooks/cf_fabric.yml
  )
}

capture() {
  local release="$1"
  log "capturing fixtures for ${release}"
  (
    cd "${REPO}"
    uv run python -m tests.system.capture --release "${release}" \
      "${CAPTURE_NODES[@]/#/--node=}"
  )
}

deploy() {
  local release="$1"
  prepare_topology
  # --cleanup so the first release starts from an empty config: the lab
  # directory may still hold startup configs saved by a previous run, and
  # feeding a newer release's config to an older one is not a supported
  # transformation.
  log "clearing any previous lab"
  clab "${release}" destroy --cleanup || true
  log "deploying at ${release}"
  clab "${release}" deploy
  wait_for_nodes
  configure_fabric
  wait_for_convergence
  "${REPO}/tests/system/traffic.sh"
  capture "${release}"
}

upgrade() {
  local release="$1"
  log "saving the running configs"
  clab "${RELEASES[0]}" save
  log "destroying the lab, keeping ${LAB_DIR}/clab-4l2s so the configs survive"
  clab "${RELEASES[0]}" destroy
  log "redeploying at ${release} - no --reconfigure, so SR Linux transforms the config"
  clab "${release}" deploy
  wait_for_nodes
  wait_for_convergence
  "${REPO}/tests/system/traffic.sh"
  capture "${release}"
}

case "${1:-all}" in
deploy) deploy "${2:?release}" ;;
upgrade) upgrade "${2:?release}" ;;
capture) capture "${2:?release}" ;;
converge)
  wait_for_nodes
  wait_for_convergence
  ;;
traffic) "${REPO}/tests/system/traffic.sh" ;;
all)
  deploy "${RELEASES[0]}"
  for release in "${RELEASES[@]:1}"; do
    upgrade "${release}"
  done
  log "done - fixtures are under tests/fixtures/releases/"
  ;;
*)
  echo "usage: $0 {all|deploy|upgrade|capture|converge|traffic} [release]" >&2
  exit 2
  ;;
esac
