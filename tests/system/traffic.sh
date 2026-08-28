#!/usr/bin/env bash
# Drive traffic through the lab services so the state-dependent reports have
# something to report on.
#
# mac, arp, nd and es-dest read learned state, not configuration: without hosts
# talking to each other they come back empty, and an empty report cannot tell a
# working gNMI path from a broken one. Pings between the multi-homed clients
# populate the MAC tables of both mac-vrfs, the ARP/ND caches of the ip-vrf
# IRBs, and the per-ES destinations on the remote leaves.
#
# Usage: traffic.sh [prefix]   (default prefix: clab-4l2s)
set -uo pipefail

PREFIX="${1:-clab-4l2s}"

# client:targets - subnet-1 is VLAN 100 (10.0.1.0/24, gateway .254),
# subnet-2 is VLAN 200 (10.0.2.0/24, gateway .254). The cross-subnet targets
# force the IRBs to resolve ARP and route between the two.
FLOWS=(
  "cl121:10.0.1.254 10.0.1.3 10.0.1.4 10.0.2.254 10.0.2.2"
  "cl123:10.0.1.254 10.0.1.2 10.0.1.4 10.0.2.2"
  "cl343:10.0.1.254 10.0.1.2 10.0.1.3 10.0.2.2"
  "cl122:10.0.2.254 10.0.2.3 10.0.1.254 10.0.1.2"
  "cl124:10.0.2.254 10.0.2.2 10.0.1.3"
  "cl341:10.0.2.254 10.0.2.2 10.0.1.4"
)

ok=0
fail=0
for flow in "${FLOWS[@]}"; do
  client="${flow%%:*}"
  for target in ${flow#*:}; do
    if docker exec "${PREFIX}-${client}" ping -c 3 -i 0.2 -W 2 -q "${target}" \
      >/dev/null 2>&1; then
      printf '  %-6s -> %-12s ok\n' "${client}" "${target}"
      ok=$((ok + 1))
    else
      printf '  %-6s -> %-12s unreachable\n' "${client}" "${target}"
      fail=$((fail + 1))
    fi
  done
done

echo "traffic: ${ok} flow(s) up, ${fail} unreachable"
# A few unreachable flows are expected - the lab gives two hosts the same
# address in subnet-2 - so only a total blackout is worth failing on.
[ "${ok}" -gt 0 ]
