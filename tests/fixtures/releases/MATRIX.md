# SR Linux release matrix

The report getters hard-code gNMI paths and the YANG structure they expect back.
Both move between SR Linux releases, and when they move the failure is usually
silent: a path that no longer carries a value leaves a column empty rather than
raising anything.

The recordings in this directory are the real gNMI exchange of every report
against a configured fabric, one file per node per release.
`tests/test_release_matrix.py` replays them through the production getters, so
every test run checks all four releases with no lab required.

## The lab

[srl-labs/intent-based-ansible-lab](https://github.com/srl-labs/intent-based-ansible-lab),
`topo.clab.yml`: four leaves, two spines, ten Linux clients.

| | |
|---|---|
| Underlay | eBGP, one ASN per leaf, spines in 65100 |
| Overlay | iBGP EVPN to both spines as route reflectors |
| Services | `subnet-1` (mac-vrf, VLAN 100), `subnet-2` (mac-vrf, VLAN 200), `ipvrf-1` (ip-vrf 2001, anycast IRBs on both) |
| Multi-homing | Five all-active ethernet segments over LACP LAGs to the clients |
| Recorded nodes | `l1` (leaf: services, LAGs, ethernet segments, learned state) and `s1` (spine: underlay only) |

`mac`, `arp`, `es-dest`, `irb` and `vxlan` read learned state, so
`tests/system/traffic.sh` pings between the clients before each capture. Without
that they come back empty, and an empty report cannot tell a working path from a
broken one.

## Re-recording

```bash
./tests/system/matrix.sh all
```

The fabric is configured **once**, on the oldest release, by the lab's own intent
playbook. Every later release inherits that configuration through SR Linux's own
transformation: the configs are saved, the image is swapped, and the lab is
redeployed *without* `--reconfigure`. That is deliberate - the reports have to
work against the datamodel a real upgrade leaves behind, not against a config
regenerated for the new release.

Individual steps are available as `matrix.sh {deploy|upgrade|capture|converge|traffic}`.

## What the datamodel did

Leaves added and removed under the 32 paths the reports read, comparing each
release to the one before it:

| Release | Added | Removed |
|---|---|---|
| 25.10.3 | 89 | 11 |
| 26.3.1 | 50 | 28 |
| 26.7.1 | 43 | 3 |

Despite that churn, every report produces **identical columns on all four
releases**. The differences worth knowing about:

### `vxlan` lost its VNI column in 25.10 (fixed)

Up to 25.3 the `state` datastore also carried the configured
`tunnel-interface/vxlan-interface/ingress/vni` and `type`. From 25.10.3 it does
not, so `fcli vxlan` showed `-` in the `ing-vni` column on every newer release
while still looking perfectly healthy. `get_vxlan` now reads the path with
datatype `all`, which returns the configured VNI and the state-only destinations
in one Get. The fixtures cover both sides of the change.

### The `bgp-rib` l3vpn variants work on none of these releases

`bgp-rib -a l3vpn-ipv4` and `-a l3vpn-ipv6` are rejected outright:

```
Path not valid - unknown element 'l3vpn-ipv4-unicast'.
Options are [ipv4-unicast, ipv6-unicast, evpn, route-target]
```

That is a schema error, not absent data: this fabric is EVPN-VXLAN, and the
`bgp-rib` model only carries the l3vpn containers on a node configured for MPLS
IP-VPN. The reports degrade to an empty table instead of failing, which is why
the tests list them as expected rejections rather than treating them as broken.
The options SR Linux offers also move: 26.3.1 adds the flowspec families, and
26.7.1 drops `afi-safi-name` from the list.

### LLDP starts running on `mgmt0` in 25.10.3

On 25.3.2 a leaf reports two LLDP neighbours, its two spines. From 25.10.3 it
reports seven, because `mgmt0` now runs LLDP too and every other node sits on the
shared containerlab management bridge. Harmless, but it is why the `lldp` row
counts jump between the first two releases.

## Coverage gaps

Four reports are empty on every release, because this fabric has nothing for
them to show rather than because anything is wrong: `nd` (no IPv6 hosts),
`static_routes` (the intent configures none) and `bgp_rib_ipv6` and `ipv6_rib`
(no IPv6 at all - their goldens are the bare `NI` column). Their paths are
accepted, so a rejection would still be caught - but the shape of their payloads
is not exercised. Adding IPv6 addressing and a static route to the intent would
close that.

That gap has cost us once: because no recorded node has an IPv6 route, the
matrix could not catch the columns of a report being taken from its first item
only, which emptied `ipv6-rib` on any fabric whose first network-instance or
first node has no IPv6 routes. `ipv4_rib` has routes on both recorded nodes and
looked fine throughout.

`ifstats` rates and the `arp`/`nd` expiry countdowns are derived from the clock.
Capture and replay both run under `tests/system/replay.deterministic_clock`, which
pins `now` to the capture time and the sample interval to its nominal value, so
those columns reproduce exactly.
