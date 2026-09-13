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
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, List

import pytest

from nornir_srl.fabric import FabricState
from nornir_srl.lenses import (
    LENSES,
    get_lens,
    lens_path,
    lens_service,
    lens_where,
    parse_destination,
    parse_vteps,
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


def _by(rows: List[Dict[str, object]], column: str, value: object) -> List[Dict]:
    return [row for row in rows if row.get(column) == value]


# --------------------------------------------------------------------------- #
# reading the pre-formatted payloads apart again
# --------------------------------------------------------------------------- #


def test_destination_reads_a_local_subinterface():
    dest = parse_destination("lag1.100")
    assert (dest.kind, dest.via, dest.local) == ("local", "lag1.100", True)


def test_destination_reads_an_irb_entry():
    assert parse_destination("irb-interface").local


def test_destination_reads_a_vtep():
    dest = parse_destination("vxlan-interface:vxlan1.101 vtep:192.168.255.2 vni:101")
    assert (dest.kind, dest.via, dest.overlay, dest.vni) == (
        "vtep",
        "192.168.255.2",
        "vxlan1.101",
        "101",
    )
    assert not dest.local


def test_destination_reads_an_ethernet_segment():
    dest = parse_destination(
        "vxlan-interface:vxlan1.101 esi:00:01:03:00:00:00:66:00:01:03"
    )
    assert dest.kind == "esi"
    assert dest.via == "00:01:03:00:00:00:66:00:01:03"


def test_destination_of_nothing_is_unknown():
    assert parse_destination("").kind == "unknown"


def test_vteps_read_the_destination_list():
    assert parse_vteps("(192.168.255.2, 101), (192.168.255.3, 101)") == [
        ("192.168.255.2", "101"),
        ("192.168.255.3", "101"),
    ]


# --------------------------------------------------------------------------- #
# where
# --------------------------------------------------------------------------- #


def test_where_finds_a_locally_learned_mac(state: FabricState):
    rows = lens_where(state, "00:C1:AB:00:01:21")
    local = _by(rows, "Found", "local")
    assert local, "the MAC the leaf learned on its own port is not reported local"
    assert local[0]["Node"] == LEAF
    assert local[0]["Via"].startswith("lag")


def _a_remote_mac(state: FabricState) -> str:
    """Any MAC the recorded fabric learned over the overlay.

    The generated MACs differ between one lab deploy and the next, so which one
    it is cannot be written down - only that there is one.
    """
    for _node, _ni, entry in state.sub_items("mac", "Fib"):
        if parse_destination(entry.get("Dest")).kind == "vtep":
            return str(entry["Address"])
    pytest.skip("this recording has no MAC learned over the overlay")


def test_where_finds_a_mac_learned_over_the_overlay(state: FabricState):
    rows = lens_where(state, _a_remote_mac(state))
    remote = _by(rows, "Found", "remote")
    assert remote, "a MAC behind a VTEP is not reported remote"
    # The VTEP is what it is behind, and it is not one of the nodes we have.
    assert all(row["Via"].startswith("192.168.255.") for row in remote)


def test_where_is_case_and_separator_insensitive(state: FabricState):
    assert lens_where(state, "00:c1:ab:00:01:21") == lens_where(
        state, "00-C1-AB-00-01-21"
    )


def test_where_resolves_an_ip_through_arp(state: FabricState):
    rows = lens_where(state, "100.64.1.16")
    arp = _by(rows, "Found", "arp")
    assert arp, "an address with an ARP binding is not resolved through it"
    assert arp[0]["Node"] == LEAF
    assert ":" in arp[0]["Detail"], "the binding does not report the MAC it resolved to"


def test_where_says_so_when_nothing_has_it(state: FabricState):
    rows = lens_where(state, "00:00:00:00:00:01")
    assert [row["Found"] for row in rows] == ["not found"]


def test_where_rejects_something_that_is_neither(state: FabricState):
    with pytest.raises(ValueError, match="neither a MAC nor an IP"):
        lens_where(state, "not-an-address")


def test_where_needs_something_to_look_for(state: FabricState):
    with pytest.raises(ValueError):
        lens_where(state, "")


def test_where_reports_a_mac_claimed_locally_by_two_nodes():
    """Two nodes owning one MAC is the finding; the recordings have one leaf."""
    state = FabricState()
    state.hostnames = {"l1": "l1", "l2": "l2"}
    state.reports = {
        "mac": {
            node: [{"NI": "subnet-1", "Fib": [{"Address": "00:C1:AB:00:01:21", "Dest": port, "Type": "learnt"}]}]
            for node, port in (("l1", "lag1.100"), ("l2", "lag7.100"))
        },
        "arp": {},
        "nd": {},
        "es": {},
    }
    rows = lens_where(state, "00:C1:AB:00:01:21")
    duplicate = _by(rows, "Found", "duplicate")
    assert len(duplicate) == 1
    assert "l1" in duplicate[0]["Node"] and "l2" in duplicate[0]["Node"]


def test_where_does_not_call_one_node_a_duplicate():
    state = FabricState()
    state.hostnames = {"l1": "l1"}
    state.reports = {
        "mac": {
            "l1": [
                {
                    "NI": "subnet-1",
                    "Fib": [
                        {"Address": "00:C1:AB:00:01:21", "Dest": "lag1.100", "Type": "learnt"}
                    ],
                }
            ]
        },
        "arp": {},
        "nd": {},
        "es": {},
    }
    assert not _by(lens_where(state, "00:C1:AB:00:01:21"), "Found", "duplicate")


# --------------------------------------------------------------------------- #
# path
# --------------------------------------------------------------------------- #


def test_path_walks_the_underlay_towards_a_vtep(state: FabricState):
    rows = lens_path(state, source=LEAF, destination="192.168.255.4")
    first = _by(rows, "Hop", 1)
    assert first, "the walk does not start on the node it was given"
    assert all(row["Node"] == LEAF for row in first)
    assert {row["Prefix"] for row in first} == {"192.168.255.4/32"}


def test_path_follows_every_ecmp_branch(state: FabricState):
    rows = lens_path(state, source=LEAF, destination="192.168.255.4")
    egress = {row["Egress"] for row in _by(rows, "Hop", 1)}
    assert len(egress) > 1, f"only one branch followed out of the leaf: {egress}"


def test_path_reaches_the_spine_it_is_cabled_to(state: FabricState):
    rows = lens_path(state, source=LEAF, destination="192.168.255.4")
    assert any(row["Node"] == SPINE for row in rows), "the walk never reaches the spine"


def test_path_stops_where_lldp_has_no_neighbour(state: FabricState):
    """The fabric has four leaves; two nodes are recorded. It has to say so."""
    rows = lens_path(state, source=LEAF, destination="192.168.255.4")
    stopped = [row for row in rows if "no LLDP neighbour" in str(row["Detail"])]
    assert stopped, "a walk off the recorded nodes does not report that it stopped"


def test_path_delivers_an_attached_address_locally(state: FabricState):
    rows = lens_path(state, source=LEAF, destination="10.0.2.51", ni="ipvrf-1")
    assert len(rows) == 1, f"an attached subnet should end the walk at once: {rows}"
    assert rows[0]["Type"] == "local"
    assert rows[0]["Egress"].startswith("irb")
    assert "delivered here" in rows[0]["Detail"]


def test_path_hands_a_vrf_lookup_off_to_the_underlay(state: FabricState):
    rows = lens_path(state, source=LEAF, destination="10.0.1.4", ni="ipvrf-1")
    overlay = [row for row in rows if str(row["Egress"]).startswith("vxlan:")]
    assert overlay, "a route resolved over the overlay does not report its tunnel"
    assert overlay[0]["NI"] == "ipvrf-1"
    # and the walk continues in the underlay, towards that VTEP
    underlay = [row for row in rows if row["NI"] == "default"]
    assert underlay, "the walk does not continue in the default instance"
    assert underlay[0]["Prefix"].startswith("192.168.255.")


def test_path_reports_a_destination_nothing_routes_to(state: FabricState):
    rows = lens_path(state, source=LEAF, destination="203.0.113.1")
    assert [row["Type"] for row in rows] == ["no route"]


def test_path_rejects_a_destination_that_is_not_an_address(state: FabricState):
    with pytest.raises(ValueError, match="not an IP address"):
        lens_path(state, source=LEAF, destination="somewhere")


def test_path_rejects_a_source_it_cannot_place(state: FabricState):
    with pytest.raises(ValueError, match="neither a node"):
        lens_path(state, source="nowhere", destination="192.168.255.4")


def test_path_starts_from_an_attached_address(state: FabricState):
    """A source given as an address starts wherever that address is attached."""
    rows = lens_path(state, source="100.64.1.16", destination="192.168.255.4")
    assert _by(rows, "Hop", 1)[0]["Node"] == LEAF


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #


def test_service_reports_the_instance_on_the_node_carrying_it(state: FabricState):
    rows = lens_service(state, "subnet-1")
    assert [row["Node"] for row in rows] == [LEAF]
    row = rows[0]
    assert row["Type"] == "mac-vrf"
    assert row["VNI"] and row["In-RT"] and row["Out-RT"]


def test_service_counts_local_and_remote_macs(state: FabricState):
    row = lens_service(state, "subnet-1")[0]
    assert "local" in row["MACs"] and "remote" in row["MACs"]


def test_service_lists_the_vteps_of_the_overlay(state: FabricState):
    row = lens_service(state, "subnet-1")[0]
    assert row["VTEPs"].count("192.168.255.") > 1


def test_service_names_the_ethernet_segments_on_it(state: FabricState):
    row = lens_service(state, "subnet-1")[0]
    assert row["ES"], "a service with multi-homed access ports names no segment"


def test_service_matches_as_a_regex(state: FabricState):
    assert len(lens_service(state, "subnet")) >= 2


def test_service_reports_an_ip_vrf_and_what_is_bound_to_it(state: FabricState):
    row = lens_service(state, "^ipvrf-1$")[0]
    assert row["Type"] == "ip-vrf"
    assert row["Interfaces"].startswith("irb")


def test_service_says_when_nothing_matches(state: FabricState):
    with pytest.raises(ValueError, match="no network-instance matching"):
        lens_service(state, "does-not-exist")


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
        assert "Node" not in lens.columns, (
            f"lens '{lens.name}' repeats the Node column the table already renders"
        )
        assert lens.params, f"lens '{lens.name}' takes no arguments"


def test_a_lens_is_found_by_either_spelling():
    assert get_lens("path") is get_lens("path")
    assert get_lens("where").name == "where"


def test_an_unknown_lens_says_so():
    with pytest.raises(KeyError, match="unknown lens"):
        get_lens("nonsense")


def test_lens_rows_only_hold_columns_the_lens_declares(state: FabricState):
    """Whatever a lens returns has to be renderable by the table it declared."""
    answers = {
        "where": lens_where(state, "00:C1:AB:00:01:21"),
        "service": lens_service(state, "subnet-1"),
        "path": lens_path(state, source=LEAF, destination="192.168.255.4"),
    }
    for name, rows in answers.items():
        allowed = {"Node", *get_lens(name).columns}
        for row in rows:
            assert set(row) <= allowed, (
                f"lens '{name}' returned {sorted(set(row) - allowed)}, "
                "which no column would render"
            )
