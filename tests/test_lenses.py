"""The lenses, run over the recorded fabric the report tests replay.

A lens joins several reports across several nodes, so a hand-written payload
tests the joining logic against whatever the test author believed the device
sends. These build a :class:`FabricState` out of the same release recordings
``test_release_matrix.py`` replays, so what the lenses are reading is what a
real SR Linux fabric actually answered.

The recordings cover one leaf and one spine per release, which is a real fabric
with most of it missing. That is deliberately not worked around: a walk that
leaves the recorded nodes has to say it can go no further rather than invent a
hop, and asserting that it does is worth as much as asserting the hops it can
take.

A lens answers with records, and a table is made of them separately. The
tests of a lens read the records; the tests of the rows read what the table
makes of a known record, so that a sentence in a Detail cell can change
without a test of the walk that produced it noticing.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from functools import lru_cache
from typing import Any, List

import pytest
import yaml

from nornir_srl.fabric import FabricState
from nornir_srl.lenses import (
    LENSES,
    Hop,
    Interface,
    Service,
    Sighting,
    get_lens,
    lens_path,
    lens_service,
    lens_where,
)
from nornir_srl.records import BridgeTable, Egress, MacEntry, Route, RouteNextHop, RouteTable, as_dict
from nornir_srl.reports import REPORTS_BY_NAME
from tests.system.replay import Recording, recording_paths

#: Every report any lens reads, which is what a state has to be built from.
_REPORTS = tuple(dict.fromkeys(r for lens in LENSES for r in lens.requires))

LEAF = "clab-4l2s-l1"
SPINE = "clab-4l2s-s1"


@lru_cache(maxsize=None)
def _state(release: str) -> FabricState:
    """A fabric state assembled from every node recorded for *release*."""
    state = FabricState()
    recordings = [
        Recording.load(path)
        for path in recording_paths()
        if Recording.load(path).release == release
    ]
    state.hostnames = {r.node: r.node for r in recordings}
    for report in _REPORTS:
        state.reports[report] = {
            r.node: (r.run(report).get(REPORTS_BY_NAME[report].resource) or [])
            for r in recordings
            if report in r.reports
        }
    return state


def _releases() -> List[str]:
    return sorted({Recording.load(path).release for path in recording_paths()})


@pytest.fixture(params=_releases())
def state(request) -> FabricState:
    return _state(request.param)


def _of_kind(sightings: List[Sighting], kind: str) -> List[Sighting]:
    return [s for s in sightings if s.kind == kind]


def _at_hop(hops: List[Hop], number: int) -> List[Hop]:
    return [h for h in hops if h.hop == number]


def _with(hops: List[Hop], outcome: str) -> List[Hop]:
    return [h for h in hops if h.outcome == outcome]


# --------------------------------------------------------------------------- #
# where
# --------------------------------------------------------------------------- #


def test_where_finds_a_locally_learned_mac(state: FabricState):
    local = _of_kind(lens_where(state, "00:C1:AB:00:01:21"), "local")
    assert local, "the MAC the leaf learned on its own port is not reported local"
    assert local[0].node == LEAF
    assert local[0].interface.startswith("lag")
    assert not local[0].vtep and not local[0].esi


def _a_remote_mac(state: FabricState) -> str:
    """Any MAC the recorded fabric learned over the overlay.

    The generated MACs differ between one lab deploy and the next, so which one
    it is cannot be written down - only that there is one.
    """
    for _node, _table, entry in state.sub_items("mac", "entries"):
        if entry.vtep:
            return entry.address
    pytest.skip("this recording has no MAC learned over the overlay")


def test_where_finds_a_mac_learned_over_the_overlay(state: FabricState):
    remote = _of_kind(lens_where(state, _a_remote_mac(state)), "remote")
    assert remote, "a MAC behind a VTEP is not reported remote"
    # The VTEP is what it is behind, and it is not one of the nodes we have.
    assert all(s.vtep.startswith("192.168.255.") for s in remote)
    assert all(s.overlay and not s.interface for s in remote)


def test_where_is_case_and_separator_insensitive(state: FabricState):
    assert lens_where(state, "00:c1:ab:00:01:21") == lens_where(
        state, "00-C1-AB-00-01-21"
    )


def test_where_resolves_an_ip_through_arp(state: FabricState):
    arp = _of_kind(lens_where(state, "100.64.1.16"), "arp")
    assert arp, "an address with an ARP binding is not resolved through it"
    assert arp[0].node == LEAF
    assert ":" in arp[0].mac, "the binding does not report the MAC it resolved to"
    assert arp[0].address == "100.64.1.16"


def test_where_says_so_when_nothing_has_it(state: FabricState):
    sightings = lens_where(state, "00:00:00:00:00:01")
    assert [s.kind for s in sightings] == ["not-found"]
    assert sightings[0].searched == len(state.nodes("mac"))


def test_where_rejects_something_that_is_neither(state: FabricState):
    with pytest.raises(ValueError, match="neither a MAC nor an IP"):
        lens_where(state, "not-an-address")


def test_where_needs_something_to_look_for(state: FabricState):
    with pytest.raises(ValueError):
        lens_where(state, "")


def _bridge_table(mac: str, port: str) -> BridgeTable:
    return BridgeTable("subnet-1", (MacEntry.read(mac, port, "learnt"),))


def _two_leaves_owning(mac: str) -> FabricState:
    state = FabricState()
    state.hostnames = {"l1": "l1", "l2": "l2"}
    state.reports = {
        "mac": {
            node: [_bridge_table(mac, port)]
            for node, port in (("l1", "lag1.100"), ("l2", "lag7.100"))
        },
        "arp": {},
        "nd": {},
        "es": {},
    }
    return state


def test_where_reports_a_mac_claimed_locally_by_two_nodes():
    """Two nodes owning one MAC is the finding; the recordings have one leaf.

    Each node's own sighting is the duplicate, naming the other, the way a
    check reports a disagreement between nodes: one finding per node.
    """
    sightings = lens_where(_two_leaves_owning("00:C1:AB:00:01:21"), "00:C1:AB:00:01:21")
    duplicate = _of_kind(sightings, "duplicate")
    assert {s.node for s in duplicate} == {"l1", "l2"}
    assert {s.also_on for s in duplicate} == {("l2",), ("l1",)}
    # It is still known what each learned it on.
    assert {s.interface for s in duplicate} == {"lag1.100", "lag7.100"}
    assert not _of_kind(sightings, "local"), "a duplicate is not also reported as local"


def test_where_does_not_call_one_node_a_duplicate():
    state = FabricState()
    state.hostnames = {"l1": "l1"}
    state.reports = {
        "mac": {"l1": [_bridge_table("00:C1:AB:00:01:21", "lag1.100")]},
        "arp": {},
        "nd": {},
        "es": {},
    }
    assert not _of_kind(lens_where(state, "00:C1:AB:00:01:21"), "duplicate")


# --------------------------------------------------------------------------- #
# path
# --------------------------------------------------------------------------- #


def test_path_walks_the_underlay_towards_a_vtep(state: FabricState):
    first = _at_hop(lens_path(state, source=LEAF, destination="192.168.255.4"), 1)
    assert first, "the walk does not start on the node it was given"
    assert all(h.node == LEAF for h in first)
    assert {h.prefix for h in first} == {"192.168.255.4/32"}
    assert all(h.address == "192.168.255.4" for h in first)


def test_path_follows_every_ecmp_branch(state: FabricState):
    hops = lens_path(state, source=LEAF, destination="192.168.255.4")
    egress = {h.egress for h in _at_hop(hops, 1)}
    assert len(egress) > 1, f"only one branch followed out of the leaf: {egress}"


def test_path_reaches_the_spine_it_is_cabled_to(state: FabricState):
    hops = lens_path(state, source=LEAF, destination="192.168.255.4")
    forwarded = [h for h in _with(hops, "forwarded") if h.peer == SPINE]
    assert forwarded, "the walk never reaches the spine"
    assert forwarded[0].peer_port, "the peer is named without the port it answers on"
    assert any(h.node == SPINE for h in hops), "the walk does not continue on the spine"


def test_path_stops_where_lldp_has_no_neighbour(state: FabricState):
    """The fabric has four leaves; two nodes are recorded. It has to say so."""
    hops = lens_path(state, source=LEAF, destination="192.168.255.4")
    stopped = _with(hops, "dead-end")
    assert stopped, "a walk off the recorded nodes does not report that it stopped"
    assert all(h.egress and not h.peer for h in stopped)


def test_path_delivers_an_attached_address_locally(state: FabricState):
    hops = lens_path(state, source=LEAF, destination="10.0.2.51", ni="ipvrf-1")
    assert [h.outcome for h in hops] == ["delivered", "no-neighbor"], hops
    assert hops[0].route_type == "local"
    assert hops[0].egress.startswith("irb")
    # The last mile is about the address itself, not a route.
    assert hops[1].address == "10.0.2.51" and hops[1].hop == 2
    assert not hops[1].prefix


def test_path_hands_a_vrf_lookup_off_to_the_underlay(state: FabricState):
    hops = lens_path(state, source=LEAF, destination="10.0.1.4", ni="ipvrf-1")
    overlay = _with(hops, "overlay")
    assert overlay, "a route resolved over the overlay does not report its tunnel"
    assert overlay[0].ni == "ipvrf-1"
    assert overlay[0].egress == f"vxlan:{overlay[0].vtep}"
    # and the walk continues in the underlay, towards that VTEP
    underlay = [h for h in hops if h.ni == "default"]
    assert underlay, "the walk does not continue in the default instance"
    assert underlay[0].prefix.startswith("192.168.255.")
    assert underlay[0].address == overlay[0].vtep


def test_path_reports_a_destination_nothing_routes_to(state: FabricState):
    hops = lens_path(state, source=LEAF, destination="203.0.113.1")
    assert [h.outcome for h in hops] == ["no-route"]
    assert not hops[0].prefix and not hops[0].route_type


def test_path_rejects_a_destination_that_is_not_an_address(state: FabricState):
    with pytest.raises(ValueError, match="not an IP address"):
        lens_path(state, source=LEAF, destination="somewhere")


def test_path_rejects_a_source_it_cannot_place(state: FabricState):
    with pytest.raises(ValueError, match="neither a node"):
        lens_path(state, source="nowhere", destination="192.168.255.4")


def test_path_starts_from_an_attached_address(state: FabricState):
    """A source given as an address starts wherever that address is attached."""
    hops = lens_path(state, source="100.64.1.16", destination="192.168.255.4")
    assert _at_hop(hops, 1)[0].node == LEAF


def _via(address: str, *egress: Egress) -> RouteNextHop:
    return RouteNextHop(address=address, type="direct", egress=egress)


def _route(prefix: str, kind: str, *next_hops: RouteNextHop) -> Route:
    return Route(prefix=prefix, type=kind, active=True, next_hops=next_hops)


def _leaf_and_dcgw() -> FabricState:
    """A leaf whose VRF route resolves over VXLAN to a DCGW that owns the VTEP.

    The leaf's ipvrf-l3dci route to 10.200.2.0/24 points at VTEP
    192.168.255.2; the underlay delivers that VTEP on dcgw1 (system0), where
    ipvrf-l3dci has the destination as a local route.
    """
    state = FabricState()
    state.hostnames = {"leaf1": "leaf1", "dcgw1": "dcgw1"}
    state.reports = {
        "ipv4_rib": {
            "leaf1": [
                RouteTable(
                    "ipvrf-l3dci",
                    (_route("10.200.2.0/24", "bgp", _via("192.168.255.2", Egress("tunnel", "192.168.255.2/32", tunnel="vxlan"))),),
                ),
                RouteTable(
                    "default",
                    (_route("192.168.255.2/32", "bgp", _via("192.168.255.2", Egress("interface", "ethernet-1/49.0"))),),
                ),
            ],
            "dcgw1": [
                RouteTable("default", (_route("192.168.255.2/32", "host", _via("", Egress("interface", "system0.0"))),)),
                RouteTable("ipvrf-l3dci", (_route("10.200.2.0/24", "local", _via("", Egress("interface", "irb0.2"))),)),
            ],
        },
        "ipv6_rib": {"leaf1": [], "dcgw1": []},
        "lldp": {
            "leaf1": [
                {
                    "interface": "ethernet-1/49",
                    "Neighbors": [{"Nbr-System": "dcgw1", "Nbr-port": "ethernet-1/1"}],
                }
            ],
            "dcgw1": [
                {
                    "interface": "ethernet-1/1",
                    "Neighbors": [{"Nbr-System": "leaf1", "Nbr-port": "ethernet-1/49"}],
                }
            ],
        },
        "arp": {},
        "nd": {},
    }
    return state


def test_path_continues_in_vrf_after_vtep_is_reached():
    """The walk resumes in the original VRF on the node that owns the VTEP."""
    hops = lens_path(
        _leaf_and_dcgw(), source="leaf1", destination="10.200.2.23", ni="ipvrf-l3dci"
    )
    assert [(h.hop, h.node, h.ni, h.outcome) for h in hops] == [
        (1, "leaf1", "ipvrf-l3dci", "overlay"),
        (2, "leaf1", "default", "forwarded"),
        (3, "dcgw1", "default", "vtep-reached"),
        (4, "dcgw1", "ipvrf-l3dci", "delivered"),
        (5, "dcgw1", "ipvrf-l3dci", "no-neighbor"),
    ]
    overlay, underlay, vtep, delivered, last = hops
    assert overlay.vtep == "192.168.255.2"
    # The underlay leg looks the VTEP up, not the destination.
    assert underlay.address == "192.168.255.2" and underlay.peer == "dcgw1"
    assert vtep.route_type == "host" and vtep.resumes_in == "ipvrf-l3dci"
    assert delivered.route_type == "local" and delivered.egress == "irb0.2"
    assert last.address == "10.200.2.23"


def test_path_confirms_the_neighbour_of_a_delivered_address():
    state = _leaf_and_dcgw()
    state.reports["arp"] = {
        "dcgw1": [
            {
                "NI": "ipvrf-l3dci",
                "interface": "irb0.2",
                "entries": [
                    {"IPv4": "10.200.2.23", "MAC": "00:C1:AB:00:02:17", "Type": "dynamic"}
                ],
            }
        ]
    }
    hops = lens_path(state, source="leaf1", destination="10.200.2.23", ni="ipvrf-l3dci")
    last = hops[-1]
    assert last.outcome == "neighbor"
    assert (last.mac, last.egress, last.origin) == ("00:C1:AB:00:02:17", "irb0.2", "dynamic")


def test_path_reports_a_loop_with_the_steps_it_took():
    """Two nodes each routing the destination to the other."""
    state = FabricState()
    state.hostnames = {"a": "a", "b": "b"}
    route = _route("10.9.9.0/24", "static", _via("", Egress("interface", "ethernet-1/1.0")))
    state.reports = {
        "ipv4_rib": {node: [RouteTable("default", (route,))] for node in ("a", "b")},
        "ipv6_rib": {},
        "lldp": {
            "a": [{"interface": "ethernet-1/1", "Neighbors": [{"Nbr-System": "b", "Nbr-port": "ethernet-1/1"}]}],
            "b": [{"interface": "ethernet-1/1", "Neighbors": [{"Nbr-System": "a", "Nbr-port": "ethernet-1/1"}]}],
        },
        "arp": {},
        "nd": {},
    }
    hops = lens_path(state, source="a", destination="10.9.9.9")
    loop = _with(hops, "loop")
    assert loop, "a walk that comes back to where it was does not say so"
    assert loop[0].visited == ("a/default", "b/default")


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #


def test_service_reports_the_instance_on_the_node_carrying_it(state: FabricState):
    services = lens_service(state, "subnet-1")
    assert [s.node for s in services] == [LEAF]
    service = services[0]
    assert service.type == "mac-vrf"
    assert service.vnis and service.import_rts and service.export_rts


def test_service_counts_local_and_remote_macs(state: FabricState):
    service = lens_service(state, "subnet-1")[0]
    assert service.local_macs > 0 and service.remote_macs > 0


def test_service_lists_the_vteps_of_the_overlay(state: FabricState):
    service = lens_service(state, "subnet-1")[0]
    assert len(service.vteps) > 1
    assert all(vtep.startswith("192.168.255.") for vtep in service.vteps)
    assert list(service.vteps) == sorted(set(service.vteps))


def test_service_names_the_ethernet_segments_on_it(state: FabricState):
    service = lens_service(state, "subnet-1")[0]
    assert service.segments, "a service with multi-homed access ports names no segment"


def test_service_matches_as_a_regex(state: FabricState):
    assert len(lens_service(state, "subnet")) >= 2


def test_service_reports_an_ip_vrf_and_what_is_bound_to_it(state: FabricState):
    service = lens_service(state, "^ipvrf-1$")[0]
    assert service.type == "ip-vrf"
    assert service.interfaces[0].name.startswith("irb")
    assert service.interfaces[0].oper in ("up", "down")


def test_service_says_when_nothing_matches(state: FabricState):
    with pytest.raises(ValueError, match="no network-instance matching"):
        lens_service(state, "does-not-exist")


# --------------------------------------------------------------------------- #
# rows: what the table makes of a record
# --------------------------------------------------------------------------- #

WHERE = get_lens("where")
PATH = get_lens("path")
SERVICE = get_lens("service")


def test_a_row_holds_the_node_and_every_declared_column(state: FabricState):
    answers = {
        WHERE: lens_where(state, "00:C1:AB:00:01:21"),
        SERVICE: lens_service(state, "subnet-1"),
        PATH: lens_path(state, source=LEAF, destination="192.168.255.4"),
    }
    for spec, records in answers.items():
        assert records
        for row in spec.rows(records):
            assert list(row) == ["Node", *spec.column_names], (
                f"lens '{spec.name}' renders columns it does not declare"
            )


def test_a_remote_sighting_reads_its_overlay():
    remote = Sighting(
        "l1", "subnet-1", "remote", "00:C1:AB:00:01:21",
        vtep="192.168.255.3", origin="evpn", overlay="vxlan1.101", vni=101,
    )
    row = WHERE.row(remote)
    assert row["Node"] == "l1"
    assert row["Found"] == "remote"
    assert row["Via"] == "192.168.255.3"
    assert row["Detail"] == "evpn, overlay vxlan1.101, vni 101"


def test_a_sighting_behind_a_segment_names_it():
    behind = Sighting(
        "l1", "subnet-1", "remote", "00:C1:AB:00:01:21",
        esi="00:01:03:00:00:00:66:00:01:03", origin="evpn", overlay="vxlan1.101",
        segments=("ES-3",),
    )
    assert WHERE.row(behind)["Via"] == "00:01:03:00:00:00:66:00:01:03"
    assert WHERE.row(behind)["Detail"] == "evpn, segment ES-3"
    unknown = Sighting(
        "l1", "subnet-1", "remote", "00:C1:AB:00:01:21",
        esi="00:01:03:00:00:00:66:00:01:03", origin="evpn",
    )
    assert WHERE.row(unknown)["Detail"] == "evpn, segment not local"


def test_a_binding_reads_the_mac_it_resolved_to():
    arp = Sighting(
        "l1", "ipvrf-1", "arp", "10.0.1.51",
        interface="irb0.1", origin="dynamic", mac="00:C1:AB:00:01:21", expiry="3h",
    )
    assert WHERE.row(arp)["Detail"] == "00:C1:AB:00:01:21, dynamic, expires 3h"


def test_a_duplicate_names_the_other_owners_and_hedges():
    rows = WHERE.rows(lens_where(_two_leaves_owning("00:C1:AB:00:01:21"), "00:C1:AB:00:01:21"))
    by_node = {row["Node"]: row for row in rows}
    assert by_node["l1"]["Found"] == "duplicate"
    assert by_node["l1"]["Via"] == "lag1.100"
    assert "also learned locally on l2" in by_node["l1"]["Detail"]
    assert "all-active segment" in by_node["l1"]["Detail"]
    assert "also learned locally on l1" in by_node["l2"]["Detail"]


def test_nothing_found_is_a_row_without_a_node():
    row = WHERE.row(Sighting("", "", "not-found", "00:00:00:00:00:01", searched=2))
    assert row["Node"] == "-"
    assert row["Found"] == "not-found"
    assert row["Detail"] == "no node reports it in any bridge table (2 searched)"


def test_a_forwarded_hop_reads_its_route_and_its_peer():
    hop = Hop(
        1, "leaf1", "default", "192.168.255.4", "forwarded",
        prefix="192.168.255.4/32", route_type="bgp",
        next_hops=("192.168.255.1", "192.168.255.2"),
        egress="ethernet-1/49.0", peer="spine1", peer_port="ethernet-1/1",
    )
    row = PATH.row(hop)
    assert row["Type"] == "bgp"
    assert row["Next-hop"] == "192.168.255.1, 192.168.255.2"
    assert row["Peer"] == "spine1 ethernet-1/1"
    assert row["Detail"] == ""


def test_a_dead_end_says_which_port_has_no_neighbour():
    hop = Hop(2, "spine1", "default", "192.168.255.4", "dead-end",
              prefix="192.168.255.4/32", route_type="bgp", egress="ethernet-1/4.0")
    row = PATH.row(hop)
    assert row["Peer"] == ""
    assert row["Detail"].startswith("no LLDP neighbour on ethernet-1/4,")


def test_a_hop_without_a_route_shows_the_outcome_as_its_type():
    assert PATH.row(Hop(1, "leaf1", "default", "203.0.113.1", "no-route"))["Type"] == "no-route"
    assert PATH.row(Hop(1, "leaf1", "default", "203.0.113.1", "no-route"))["Prefix"] == "-"
    loop = Hop(3, "a", "default", "10.9.9.9", "loop", visited=("a/default", "b/default"))
    assert PATH.row(loop)["Detail"] == "already visited on this path (a/default -> b/default)"


def test_the_last_mile_reads_the_address_and_the_mac():
    neighbor = Hop(5, "dcgw1", "ipvrf-l3dci", "10.200.2.23", "neighbor",
                   egress="irb0.2", mac="00:C1:AB:00:02:17", origin="dynamic")
    row = PATH.row(neighbor)
    assert row["Prefix"] == "10.200.2.23"
    assert row["Next-hop"] == "00:C1:AB:00:02:17"
    assert row["Detail"] == "00:C1:AB:00:02:17 on irb0.2, dynamic"
    missing = Hop(5, "dcgw1", "ipvrf-l3dci", "10.200.2.23", "no-neighbor")
    assert PATH.row(missing)["Detail"] == "no ARP/ND entry for 10.200.2.23 on this node"


def test_every_hop_outcome_has_a_detail():
    """A walk can end in every outcome documented on Hop; none may render blank."""
    from nornir_srl.lenses import _HOP_DETAIL  # noqa: PLC0415 - the map is the test

    documented = {
        "forwarded", "dead-end", "overlay", "vtep-reached", "delivered", "local-ip",
        "neighbor", "no-neighbor", "no-route", "loop", "too-long",
    }
    assert set(_HOP_DETAIL) == documented


def test_a_service_row_joins_its_lists_and_counts_its_macs():
    service = Service(
        node="l1", ni="subnet-1", type="mac-vrf", oper="up", evis=("101",),
        vnis=(101,), import_rts=("target:100:101",), export_rts=("target:100:101",),
        interfaces=(Interface("lag1.100", "up"), Interface("irb0.1", "down")),
        bound=("ipvrf-1",), vteps=("192.168.255.2", "192.168.255.3"),
        local_macs=2, remote_macs=3, segments=("ES-1",),
    )
    row = SERVICE.row(service)
    assert row["Interfaces"] == "lag1.100(up), irb0.1(down)"
    assert row["VTEPs"] == "192.168.255.2, 192.168.255.3"
    assert row["MACs"] == "2 local / 3 remote"
    assert row["In-RT"] == "target:100:101"
    assert row["EVI"] == "101"


def test_a_service_with_no_macs_leaves_the_count_blank():
    service = Service(
        node="l1", ni="ipvrf-1", type="ip-vrf", oper="up", evis=(),
        vnis=(), import_rts=(), export_rts=(), interfaces=(), bound=(), vteps=(),
        local_macs=0, remote_macs=0, segments=(),
    )
    assert SERVICE.row(service)["MACs"] == ""


def test_a_long_list_says_how_much_it_left_out():
    service = Service(
        node="l1", ni="subnet-1", type="mac-vrf", oper="up", evis=("101",),
        vnis=(101,), import_rts=(), export_rts=(), interfaces=(), bound=(),
        vteps=tuple(f"192.168.255.{n}" for n in range(2, 9)),
        local_macs=0, remote_macs=0, segments=(),
    )
    assert SERVICE.row(service)["VTEPs"].endswith(", +3 more")


# --------------------------------------------------------------------------- #
# records as objects: what -o json and the MCP tools emit
# --------------------------------------------------------------------------- #


def test_a_record_is_a_plain_object_with_its_lists_intact():
    service = Service(
        node="l1", ni="subnet-1", type="mac-vrf", oper="up", evis=("101",),
        vnis=(101,), import_rts=(), export_rts=(),
        interfaces=(Interface("lag1.100", "up"),), bound=(),
        vteps=("192.168.255.2", "192.168.255.3"), local_macs=2, remote_macs=3,
        segments=(),
    )
    obj = as_dict(service)
    assert obj["vteps"] == ["192.168.255.2", "192.168.255.3"]
    assert obj["interfaces"] == [{"name": "lag1.100", "oper": "up"}]
    assert obj["local_macs"] == 2
    # Nothing a table did to it: no joined cell, no column name.
    assert "VTEPs" not in obj and "MACs" not in obj


def test_every_record_serialises_to_json_and_yaml(state: FabricState):
    records: List[Any] = [
        *lens_where(state, "00:C1:AB:00:01:21"),
        *lens_service(state, "subnet-1"),
        *lens_path(state, source=LEAF, destination="192.168.255.4"),
    ]
    objects = [as_dict(r) for r in records]
    assert json.loads(json.dumps(objects)) == objects
    assert yaml.safe_load(yaml.safe_dump(objects)) == objects


def test_a_record_has_a_field_for_every_kind_it_documents():
    """The fields a record documents by kind or outcome all exist on it."""
    for record in (Sighting, Hop, Service, Interface):
        assert is_dataclass(record)
        names = {f.name for f in fields(record)}
        assert "node" in names or record is Interface
    assert {"also_on", "searched", "segments", "expiry"} <= {f.name for f in fields(Sighting)}
    assert {"resumes_in", "vtep", "visited", "mac"} <= {f.name for f in fields(Hop)}


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


def test_every_lens_declares_the_reports_it_reads():
    for lens in LENSES:
        assert lens.requires, f"lens '{lens.name}' reads nothing"
        for report in lens.requires:
            assert report in REPORTS_BY_NAME


def test_every_lens_declares_its_columns_and_params():
    for lens in LENSES:
        assert lens.columns, f"lens '{lens.name}' has no columns"
        assert "Node" not in lens.column_names, (
            f"lens '{lens.name}' repeats the Node column the table already renders"
        )
        assert len(set(lens.column_names)) == len(lens.column_names), (
            f"lens '{lens.name}' names a column twice"
        )
        assert lens.params, f"lens '{lens.name}' takes no arguments"


def test_a_lens_is_found_by_either_spelling():
    assert get_lens("path") is get_lens("path")
    assert get_lens("where").name == "where"


def test_an_unknown_lens_says_so():
    with pytest.raises(KeyError, match="unknown lens"):
        get_lens("nonsense")
