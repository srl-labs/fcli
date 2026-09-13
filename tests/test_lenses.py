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
    coerce_lens_params,
    get_lens,
    graph_path,
    lens_path,
    lens_service,
    lens_where,
    tree_path,
    tree_service,
    tree_where,
)
from nornir_srl.records import (
    BgpVpnInstance,
    BridgeTable,
    Egress,
    MacEntry,
    NetworkInstance,
    Route,
    RouteNextHop,
    RouteTable,
    as_dict,
)
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


def test_where_finds_an_address_a_node_has_configured(state: FabricState):
    """A system address is nobody's neighbour; it is somebody's own."""
    sightings = lens_where(state, "192.168.255.1")
    configured = _of_kind(sightings, "configured")
    assert [(s.node, s.ni, s.interface, s.prefix) for s in configured] == [
        (LEAF, "default", "system0.0", "192.168.255.1/32")
    ]
    assert get_lens("where").row(configured[0])["Detail"] == "192.168.255.1/32 configured on system0.0"
    # It resolved to no MAC, and that is the whole answer rather than nothing.
    assert not _of_kind(sightings, "not-found")


def test_where_answers_an_ip_nothing_has_rather_than_saying_nothing(state: FabricState):
    """An IP with no interface and no binding used to come back as an empty list."""
    (only,) = lens_where(state, "203.0.113.9")
    assert only.kind == "not-found" and only.address == "203.0.113.9"
    assert only.searched == len(state.nodes("ni"))
    assert "no ARP or ND entry" in get_lens("where").row(only)["Detail"]


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
    overlay = _with(hops, "tunnel")
    assert overlay, "a route resolved over the overlay does not report its tunnel"
    assert overlay[0].ni == "ipvrf-1"
    assert (overlay[0].tunnel, overlay[0].egress) == ("vxlan", f"vxlan:{overlay[0].endpoint}")
    # and the walk continues in the underlay, towards that VTEP
    underlay = [h for h in hops if h.ni == "default"]
    assert underlay, "the walk does not continue in the default instance"
    assert underlay[0].prefix.startswith("192.168.255.")
    assert underlay[0].address == overlay[0].endpoint


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
        (1, "leaf1", "ipvrf-l3dci", "tunnel"),
        (2, "leaf1", "default", "forwarded"),
        (3, "dcgw1", "default", "endpoint-reached"),
        (4, "dcgw1", "ipvrf-l3dci", "delivered"),
        (5, "dcgw1", "ipvrf-l3dci", "no-neighbor"),
    ]
    overlay, underlay, vtep, delivered, last = hops
    assert (overlay.tunnel, overlay.endpoint) == ("vxlan", "192.168.255.2")
    # The underlay leg looks the VTEP up, not the destination.
    assert underlay.address == "192.168.255.2" and underlay.peer == "dcgw1"
    assert vtep.route_type == "host" and vtep.resumes_in == "ipvrf-l3dci"
    assert delivered.route_type == "local" and delivered.egress == "irb0.2"
    assert last.address == "10.200.2.23"


def _dci_over_mpls() -> FabricState:
    """A leaf, a DC gateway and a WAN gateway: VXLAN to the first, LDP to the second.

    dcgw1 carries the tenant in ``ipvrf-l3dci`` and sends it over an LDP
    tunnel to dcgw3's WAN loopback 192.0.3.7; dcgw3 calls the same VRF
    ``tenant-a`` - only the route-target ties the two - and has the
    destination attached there.
    """
    state = FabricState()
    state.hostnames = {"leaf1": "leaf1", "dcgw1": "dcgw1", "p1": "p1", "dcgw3": "dcgw3"}
    state.reports = {
        "ipv4_rib": {
            "leaf1": [
                RouteTable("ipvrf-l3dci", (_route("10.200.2.0/24", "bgp-evpn", _via("192.0.2.8", Egress("tunnel", "192.0.2.8/32", tunnel="vxlan"))),)),
                RouteTable("default", (_route("192.0.2.8/32", "bgp", _via("", Egress("interface", "ethernet-1/1.0"))),)),
            ],
            "dcgw1": [
                RouteTable("default", (
                    _route("192.0.2.8/32", "host", _via("", Egress("interface", "system0.0"))),
                    _route("192.0.3.7/32", "isis", _via("10.255.0.1", Egress("interface", "ethernet-1/5.0"))),
                )),
                RouteTable("ipvrf-l3dci", (_route("10.200.2.0/24", "bgp-ipvpn", _via("192.0.3.7", Egress("tunnel", "192.0.3.7/32", tunnel="ldp"))),)),
            ],
            "p1": [RouteTable("default", (_route("192.0.3.7/32", "isis", _via("10.255.0.6", Egress("interface", "ethernet-1/2.0"))),))],
            "dcgw3": [
                RouteTable("default", (_route("192.0.3.7/32", "host", _via("", Egress("interface", "lo0.0"))),)),
                RouteTable("tenant-a", (_route("10.200.2.0/24", "local", _via("", Egress("interface", "irb0.2"))),)),
            ],
        },
        "ipv6_rib": {},
        "ni": {
            "dcgw1": [NetworkInstance("ipvrf-l3dci", "ip-vrf", "up", instances=(
                BgpVpnInstance(1, ("3000:3000",), ("3000:3000",)), BgpVpnInstance(2, ("65000:3000",), ("65000:3000",))))],
            "dcgw3": [
                NetworkInstance("tenant-b", "ip-vrf", "up", instances=(BgpVpnInstance(2, ("65000:3001",), ("65000:3001",)),)),
                NetworkInstance("tenant-a", "ip-vrf", "up", instances=(BgpVpnInstance(2, ("65000:3000",), ("65000:3000",)),)),
            ],
        },
        "lldp": {
            "leaf1": [{"interface": "ethernet-1/1", "Neighbors": [{"Nbr-System": "dcgw1", "Nbr-port": "ethernet-1/1"}]}],
            "dcgw1": [{"interface": "ethernet-1/5", "Neighbors": [{"Nbr-System": "p1", "Nbr-port": "ethernet-1/1"}]}],
            "p1": [{"interface": "ethernet-1/2", "Neighbors": [{"Nbr-System": "dcgw3", "Nbr-port": "ethernet-1/5"}]}],
            "dcgw3": [],
        },
        "arp": {"dcgw3": [{"NI": "tenant-a", "interface": "irb0.2", "entries": [{"IPv4": "10.200.2.21", "MAC": "00:C1:AB:00:02:15", "Type": "dynamic"}]}]},
        "nd": {},
    }
    return state


def test_path_follows_mpls_to_the_far_gateway_and_resumes_in_its_vrf():
    """VXLAN to the DC gateway, LDP to the WAN gateway, into the VRF that
    imports the route-target - not the same name - and out to the host."""
    hops = lens_path(_dci_over_mpls(), source="leaf1", destination="10.200.2.21", ni="ipvrf-l3dci")
    assert [(h.hop, h.node, h.ni, h.outcome) for h in hops] == [
        (1, "leaf1", "ipvrf-l3dci", "tunnel"),
        (2, "leaf1", "default", "forwarded"),
        (3, "dcgw1", "default", "endpoint-reached"),
        (4, "dcgw1", "ipvrf-l3dci", "tunnel"),
        (5, "dcgw1", "default", "forwarded"),
        (6, "p1", "default", "forwarded"),
        (7, "dcgw3", "default", "endpoint-reached"),
        (8, "dcgw3", "tenant-a", "delivered"),
        (9, "dcgw3", "tenant-a", "neighbor"),
    ]
    mpls = hops[3]
    assert (mpls.tunnel, mpls.endpoint, mpls.egress) == ("ldp", "192.0.3.7", "ldp:192.0.3.7")
    assert hops[6].resumes_in == "tenant-a", "the far-end VRF is found by route-target"
    assert hops[8].mac == "00:C1:AB:00:02:15"


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
        "forwarded", "dead-end", "tunnel", "endpoint-reached", "delivered", "local-ip",
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
# the tree: what the browser makes of the records
# --------------------------------------------------------------------------- #


def test_where_folds_sightings_into_a_card_per_address_with_a_node_inside():
    sightings = lens_where(_two_leaves_owning("00:C1:AB:00:01:21"), "00:C1:AB:00:01:21")
    (card,) = tree_where(sightings)
    assert card.title == "00:C1:AB:00:01:21"
    assert card.subtitle == "duplicate on 2 nodes"
    assert (card.state, card.badge) == ("warn", "2 nodes")
    assert [e.title for e in card.entries] == ["l1", "l2"]
    (item,) = card.entries[0].items
    assert (item.title, item.state) == ("duplicate in subnet-1", "warn")
    by_label = {d.label: d for d in item.details}
    assert by_label["Interface"].value == "lag1.100"
    assert (by_label["Also learned locally on"].value, by_label["Also learned locally on"].state) == (("l2",), "warn")


def test_where_says_not_found_as_a_card_with_nothing_inside(state: FabricState):
    (card,) = tree_where(lens_where(state, "00:00:00:00:00:01"))
    assert (card.badge, card.state, card.entries) == ("not found", "down", ())
    assert "searched" in card.subtitle


def test_where_keeps_the_address_binding_and_the_mac_as_separate_cards(state: FabricState):
    cards = tree_where(lens_where(state, "100.64.1.16"))
    assert cards[0].title == "100.64.1.16" and cards[0].state == "up"
    assert cards[0].entries[0].items[0].title == "arp in default"


def test_path_folds_the_walk_into_a_card_per_hop():
    hops = lens_path(_leaf_and_dcgw(), source="leaf1", destination="10.200.2.23", ni="ipvrf-l3dci")
    cards = tree_path(hops)
    assert [c.title for c in cards] == ["Hop 1", "Hop 2", "Hop 3", "Hop 4", "Hop 5"]
    assert [c.subtitle for c in cards] == ["1 tunnel", "1 forwarded", "1 endpoint-reached", "1 delivered", "1 no-neighbor"]
    # A hop that only goes on has no verdict; reaching and being delivered are
    # good; a walk that stops is a fault.
    assert [c.state for c in cards] == ["", "", "up", "up", "down"]
    assert [e.title for c in cards for e in c.entries] == ["leaf1", "leaf1", "dcgw1", "dcgw1", "dcgw1"]
    overlay = cards[0].entries[0].items[0]
    assert overlay.title == "ipvrf-l3dci: 10.200.2.0/24"
    assert {d.label: d.value for d in overlay.details}["Egress"] == "vxlan:192.168.255.2"


def test_path_graph_draws_the_walk_hop_by_hop_with_its_fan_out(state: FabricState):
    """Every ECMP branch out of the leaf is an edge, and they converge on the spine."""
    graph = graph_path(lens_path(state, source=LEAF, destination="192.168.255.4"))
    assert graph["destination"] == "192.168.255.4"
    first = [n for n in graph["nodes"] if n["hop"] == 1]
    assert [n["title"] for n in first] == [LEAF]
    out = [e for e in graph["edges"] if e["from"] == first[0]["id"]]
    assert len(out) > 1, "the fan-out out of the leaf is not drawn"
    assert {e["label"] for e in out} == {h.egress for h in lens_path(state, source=LEAF, destination="192.168.255.4") if h.hop == 1}
    spine = next(n for n in graph["nodes"] if n["node"] == SPINE)
    assert any(e["to"] == spine["id"] for e in out)
    # The branch towards the spine that is not recorded goes out of its port
    # to a stop, drawn red; a lookup that found no route at all has no edge out.
    stops = [n for n in graph["nodes"] if n["title"] == "no neighbour"]
    assert stops and all(n["state"] == "down" for n in stops)
    assert any(e["to"] == stops[0]["id"] and e["state"] == "down" for e in out)
    assert not any(e["from"] == stops[0]["id"] for e in graph["edges"])


def test_path_graph_follows_the_dci_walk_into_the_host():
    graph = graph_path(lens_path(_dci_over_mpls(), source="leaf1", destination="10.200.2.21", ni="ipvrf-l3dci"))
    assert [n["title"] for n in graph["nodes"]] == [
        "leaf1", "leaf1", "dcgw1", "dcgw1", "dcgw1", "p1", "dcgw3", "dcgw3", "10.200.2.21",
    ]
    labels = [e["label"] for e in graph["edges"]]
    assert labels == [
        "vxlan:192.0.2.8", "ethernet-1/1.0", "into ipvrf-l3dci", "ldp:192.0.3.7",
        "ethernet-1/5.0", "ethernet-1/2.0", "into tenant-a", "irb0.2",
    ]
    host = graph["nodes"][-1]
    assert (host["subtitle"], host["state"]) == ("00:C1:AB:00:02:15 on irb0.2", "up")
    # An underlay lookup says what it is chasing; a VRF lookup does not repeat the destination.
    assert graph["nodes"][1]["subtitle"] == "default · 192.0.2.8"
    assert graph["nodes"][0]["subtitle"] == "ipvrf-l3dci"


def test_path_graph_of_nothing_is_empty():
    assert graph_path([]) == {"nodes": [], "edges": [], "destination": ""}


def test_service_folds_the_transpose_into_a_card_per_service_and_flags_disagreement():
    agree = Service("l1", "subnet-1", "mac-vrf", "up", ("101",), (101,), ("100:101",), ("100:101",), (Interface("lag1.100", "up"),), (), ("192.168.255.2",), 1, 2, ())
    differ = Service("l2", "subnet-1", "mac-vrf", "down", ("101",), (102,), ("100:101",), ("100:101",), (), (), (), 0, 0, ())
    (card,) = tree_service([agree, differ])
    assert card.title == "subnet-1" and card.icon == "🌉"
    assert card.subtitle == "mac-vrf, EVI 101 - nodes disagree on VNI"
    assert (card.state, card.badge) == ("down", "2 nodes")
    assert [(e.title, e.state) for e in card.entries] == [("l1", "up"), ("l2", "down")]
    details = {d.label: d for d in card.entries[0].items[0].details}
    assert details["Interfaces"].value == (("lag1.100", "up"),)
    assert details["MACs"].value == "1 local / 2 remote"
    assert card.state == "down"
    assert tree_service([agree])[0].state == "up"


def test_service_tree_does_not_hold_nodes_in_different_underlays_to_each_other():
    dc1 = Service("leaf1", "bd", "mac-vrf", "up", ("201",), (201,), ("65000:201",), ("65000:201",), (), (), (), 0, 0, (),
                  instances=(BgpVpnInstance(1, ("65000:201",), ("65000:201",)),), site="1")
    dc2 = Service("leaf5", "bd", "mac-vrf", "up", ("202",), (202,), ("65000:202",), ("65000:202",), (), (), (), 0, 0, (),
                  instances=(BgpVpnInstance(1, ("65000:202",), ("65000:202",)),), site="2")
    (card,) = tree_service([dc1, dc2])
    assert card.subtitle == "mac-vrf, EVI 201, in 2 underlays"
    assert card.state == "up"
    assert {d.label: d.value for d in card.entries[0].items[0].details}["Underlay"] == "1"


def test_service_lens_numbers_the_underlays_a_service_is_carried_in(state: FabricState):
    """One recorded fabric is one underlay, so no site is numbered."""
    assert {s.site for s in lens_service(state, "subnet-1")} == {""}


def test_service_tree_lets_a_gateway_carry_its_wan_side_instance():
    leaf = Service("leaf1", "ipvrf-1", "ip-vrf", "up", ("3000",), (3000,), ("3000:3000",), ("3000:3000",), (), (), (), 0, 0, (),
                   instances=(BgpVpnInstance(1, ("3000:3000",), ("3000:3000",)),))
    gateway = Service("dcgw1", "ipvrf-1", "ip-vrf", "up", ("3000",), (3000,), ("3000:3000", "65000:3000"), ("3000:3000", "65000:3000"), (), (), (), 0, 0, (),
                      instances=(BgpVpnInstance(1, ("3000:3000",), ("3000:3000",)), BgpVpnInstance(2, ("65000:3000",), ("65000:3000",))))
    (card,) = tree_service([leaf, gateway])
    assert "disagree" not in card.subtitle and card.state == "up"


def test_every_lens_has_a_tree_and_the_params_it_cannot_do_without():
    for lens in LENSES:
        assert callable(lens.tree)
        assert any(p.required for p in lens.params), f"lens '{lens.name}' requires nothing"
        assert lens.on("server")


def test_a_lens_refuses_to_be_asked_nothing():
    with pytest.raises(ValueError, match="needs address"):
        coerce_lens_params(get_lens("where"), {})
    assert coerce_lens_params(get_lens("path"), {"source": "leaf1", "destination": "10.0.0.1"}) == {
        "source": "leaf1",
        "destination": "10.0.0.1",
    }
    with pytest.raises(ValueError, match="not an IP address"):
        coerce_lens_params(get_lens("path"), {"source": "leaf1", "destination": "nowhere"})


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
    assert {"resumes_in", "tunnel", "endpoint", "visited", "mac"} <= {f.name for f in fields(Hop)}


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
