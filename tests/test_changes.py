"""What changed between two readings of a fabric, and what a timeline makes of it."""

from typing import Any, Dict

import pytest

from nornir_srl.changes import (
    ABSENT,
    ERROR,
    INFO,
    OK,
    WARNING,
    Change,
    diff_fabric,
    diff_findings,
    flaps,
    node_change,
    parse_since,
    settled_findings,
)
from nornir_srl.checks import CHECKS_BY_NAME, Finding
from nornir_srl.fabric import FabricState
from nornir_srl.records import (
    BgpPeers,
    BridgeTable,
    EthernetSegment,
    Association,
    Candidate,
    Family,
    Interface,
    LldpInterface,
    LldpNeighbor,
    MacEntry,
    Neighbor,
    SubinterfaceState,
)


def fabric(**reports: Dict[str, Any]) -> FabricState:
    return FabricState(reports=dict(reports))


def peers(*neighbors: Neighbor) -> list:
    return [BgpPeers("default", tuple(neighbors))]


def session(peer: str, state: str = "established", evpn: int = 100) -> Neighbor:
    return Neighbor(peer, state, families=(Family("evpn", oper="up", received=evpn),))


def summary(changes):
    return [(c.node, c.kind, c.subject, c.before, c.after, c.severity) for c in changes]


def test_a_session_going_down_is_an_error_and_coming_back_is_ok():
    up = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1"))})
    down = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1", "active"))})
    assert summary(diff_fabric(up, down, at=1)) == [
        ("leaf1", "bgp", "default/10.0.0.1", "established", "active", ERROR)
    ]
    assert summary(diff_fabric(down, up, at=2)) == [
        ("leaf1", "bgp", "default/10.0.0.1", "active", "established", OK)
    ]


def test_a_session_that_disappears_is_a_failure_one_that_appears_is_news():
    one = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1"))})
    two = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1"), session("10.0.0.2"))})
    appeared = diff_fabric(one, two, at=1)
    assert ("leaf1", "bgp", "default/10.0.0.2", ABSENT, "established", OK) in summary(appeared)
    gone = diff_fabric(two, one, at=1)
    assert ("leaf1", "bgp", "default/10.0.0.2", "established", ABSENT, ERROR) in summary(gone)


def test_a_node_that_did_not_answer_is_not_a_node_whose_sessions_all_went():
    before = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1")), "leaf2": peers(session("10.0.0.9"))})
    after = fabric(bgp_peers={"leaf2": peers(session("10.0.0.9"))})
    assert diff_fabric(before, after) == []


def test_a_route_count_is_news_only_when_it_halves_or_goes_to_or_from_nothing():
    base = fabric(bgp_peers={"leaf1": peers(session("10.0.0.1", evpn=100))})

    def to(count):
        return diff_fabric(base, fabric(bgp_peers={"leaf1": peers(session("10.0.0.1", evpn=count))}), at=1)

    assert to(92) == []  # the fabric re-converging somewhere else
    assert to(70) == []  # a real move, but the echo of a change reported elsewhere
    (halved,) = to(40)
    assert (halved.kind, halved.severity, halved.detail) == ("bgp-routes", WARNING, "received routes fell from 100 to 40")
    (emptied,) = to(0)
    assert emptied.severity == WARNING


def test_a_port_going_down_and_an_lldp_neighbour_lost():
    def reading(oper: str, neighbors: tuple) -> FabricState:
        return fabric(
            subif={"leaf1": [Interface("ethernet-1/1", (SubinterfaceState("ethernet-1/1.0", oper=oper),))]},
            lldp={"leaf1": [LldpInterface("ethernet-1/1", neighbors)]},
        )

    before = reading("up", (LldpNeighbor("spine1", "ethernet-1/1"),))
    after = reading("down", ())
    assert summary(diff_fabric(before, after, at=1)) == [
        ("leaf1", "interface", "ethernet-1/1.0", "up", "down", ERROR),
        ("leaf1", "lldp", "ethernet-1/1", "spine1 ethernet-1/1", ABSENT, WARNING),
    ]


def test_a_mac_that_moves_is_news_one_that_ages_out_is_not():
    def table(*entries):
        return fabric(mac={"leaf1": [BridgeTable("macvrf-1", tuple(entries))]})

    here = MacEntry.read("00:00:00:00:01:11", "ethernet-1/3.100", "learnt")
    there = MacEntry.read("00:00:00:00:01:11", "vxlan-interface:vxlan0.100 vtep:192.0.2.13 vni:100", "evpn")
    (moved,) = diff_fabric(table(here), table(there), at=1)
    assert (moved.kind, moved.subject, moved.before, moved.after, moved.severity) == (
        "mac", "macvrf-1 00:00:00:00:01:11", "ethernet-1/3.100", "vtep 192.0.2.13", INFO
    )
    assert diff_fabric(table(here), table(), at=1) == []


def test_a_designated_forwarder_that_moves_is_a_warning():
    def segment(df: str) -> FabricState:
        candidates = tuple(Candidate(a, designated=a == df) for a in ("192.0.2.11", "192.0.2.12"))
        es = EthernetSegment("ES-01", "00:01", "local", "all-active", "up", associations=(Association("macvrf-1", candidates),))
        return fabric(es={"leaf1": [es]})

    (change,) = diff_fabric(segment("192.0.2.11"), segment("192.0.2.12"), at=1)
    assert (change.kind, change.severity) == ("es-df", WARNING)


def test_findings_raised_and_cleared():
    bgp = Finding("bgp_down", "error", "leaf1", "default/10.0.0.1", "session is active")
    mtu = Finding("mtu_mismatch", "error", "leaf1", "ethernet-1/1.0", "9000 vs 1500")
    changes = diff_findings([bgp], [mtu], at=1)
    assert summary(changes) == [
        ("leaf1", "finding", "mtu_mismatch ethernet-1/1.0", ABSENT, "error", "error"),
        ("leaf1", "finding", "bgp_down default/10.0.0.1", "error", ABSENT, OK),
    ]


def test_a_finding_that_blinks_for_one_reading_never_reaches_the_timeline():
    blink = Finding("itf_errors", "warning", "leaf1", "ethernet-1/3", "1 discarded packet")
    changes, raised = settled_findings({}, [], [blink], at=1)
    assert changes == [] and raised == {}
    changes, raised = settled_findings(raised, [blink], [], at=2)
    assert changes == [] and raised == {}


def test_a_finding_that_lasts_two_readings_is_raised_once_and_cleared_once():
    down = Finding("bgp_down", "error", "leaf1", "default/10.0.0.1", "session is active")
    _, raised = settled_findings({}, [], [down], at=1)
    changes, raised = settled_findings(raised, [down], [down], at=2)
    assert summary(changes) == [("leaf1", "finding", "bgp_down default/10.0.0.1", ABSENT, "error", "error")]
    changes, raised = settled_findings(raised, [down], [down], at=3)
    assert changes == []
    changes, raised = settled_findings(raised, [down], [], at=4)
    assert [c.severity for c in changes] == [OK] and raised == {}


def test_a_node_that_stops_answering():
    assert node_change("leaf1", False, at=1).severity == ERROR
    assert node_change("leaf1", True, at=2).severity == OK


# --------------------------------------------------------------------------- #
# flaps
# --------------------------------------------------------------------------- #


def _flip(at: float, before: str, after: str, kind: str = "bgp", subject: str = "default/10.0.0.1") -> Change:
    return Change(at, "leaf1", kind, subject, before, after, ERROR)


def test_three_transitions_in_the_window_are_a_flap():
    changes = [
        _flip(100, "established", "active"),
        _flip(200, "active", "established"),
        _flip(300, "established", "active"),
    ]
    (flap,) = flaps(changes, now=400)
    assert (flap.kind, flap.count, flap.values) == ("bgp", 3, ("established", "active"))
    assert flaps(changes, now=400, threshold=4) == []
    assert flaps(changes, now=100 + 601 + 200) == []  # the first two have left the window


def test_route_counts_and_findings_do_not_flap():
    changes = [_flip(t, "1", "2", kind="bgp-routes") for t in (1, 2, 3)]
    changes += [_flip(t, ABSENT, "error", kind="finding") for t in (1, 2, 3)]
    assert flaps(changes, now=10) == []


def test_the_flapping_check_reads_the_timeline():
    moves = [
        Change(t, "leaf1", "mac", "macvrf-1 00:00:00:00:01:11", a, b, INFO)
        for t, a, b in ((1, "ethernet-1/3.100", "ethernet-1/4.100"), (2, "ethernet-1/4.100", "ethernet-1/3.100"), (3, "ethernet-1/3.100", "ethernet-1/4.100"))
    ]
    import time

    now = time.time()
    state = FabricState(changes=[Change(now - 10 + c.at, c.node, c.kind, c.subject, c.before, c.after, c.severity) for c in moves])
    (finding,) = CHECKS_BY_NAME["flapping"].run(state)
    assert (finding.node, finding.subject) == ("leaf1", "macvrf-1/00:00:00:00:01:11")
    assert "moved 3 times between ethernet-1/3.100, ethernet-1/4.100" in finding.detail
    assert CHECKS_BY_NAME["flapping"].run(FabricState()) == []


# --------------------------------------------------------------------------- #
# since
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,seconds",
    [("15m", 900), ("2h", 7200), ("90s", 90), ("1d", 86400), ("10", 600), (" 5M ", 300)],
)
def test_since_takes_a_time_span(text, seconds):
    assert parse_since(text, now=10_000) == 10_000 - seconds


def test_since_empty_means_everything_and_nonsense_is_an_error():
    assert parse_since("") is None
    assert parse_since(None) is None
    with pytest.raises(ValueError):
        parse_since("yesterday")
