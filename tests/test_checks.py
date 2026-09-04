"""Tests for the fabric sanity checks.

Each check is a pure function over the payloads the report getters return, so a
test is a fabric written out as those payloads and the findings it should
produce. The payload shapes here mirror the getters exactly - ``bgp_peers``
really does key a neighbour address as ``1_peer`` and name a column
``"EVPN\\nR/A/T"`` - because a check that reads a field by the wrong name would
otherwise pass here and find nothing on a real fabric.
"""

from typing import Any, Dict, List

from nornir_srl import checks as checks_module
from nornir_srl.checks import (
    CHECKS,
    CHECKS_BY_NAME,
    CHECKS_COLUMNS,
    ERROR,
    REQUIRED_REPORTS,
    WARNING,
    Check,
    FabricState,
    run_checks,
)
from nornir_srl.reports import REPORTS_BY_NAME


def fabric(**reports: Dict[str, Any]) -> FabricState:
    """A fabric whose nodes are the ones named in the reports given."""
    state = FabricState(reports=dict(reports))
    state.hostnames = {
        node: node for report in reports.values() for node in report
    }
    return state


def run(check: str, state: FabricState) -> List[Dict[str, Any]]:
    return [f.as_row() for f in CHECKS_BY_NAME[check].run(state)]


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


def test_check_names_are_unique():
    assert len({c.name for c in CHECKS}) == len(CHECKS)


def test_every_check_reads_reports_that_exist():
    for check in CHECKS:
        assert check.requires, f"{check.name} reads no report"
        for report in check.requires:
            assert report in REPORTS_BY_NAME, (
                f"{check.name} reads '{report}', which is not a report"
            )


def test_required_reports_is_what_the_checks_ask_for():
    assert set(REQUIRED_REPORTS) == {r for c in CHECKS for r in c.requires}


def test_a_finding_fills_every_column():
    findings = run("bgp_down", fabric(bgp_peers=_peers(state="active")))
    assert list(findings[0]) == list(CHECKS_COLUMNS)


# --------------------------------------------------------------------------- #
# BGP
# --------------------------------------------------------------------------- #


def _peers(state: str = "established", **overrides: Any) -> Dict[str, Any]:
    peer = {
        "1_peer": "10.0.0.2",
        "peer-as": 65002,
        "state": state,
        "group": "spines",
        "U4\nR/A/T": "5/5/3",
        "U6\nR/A/T": "-",
        "EVPN\nR/A/T": "12/12/6",
        "VPNv4\nR/A/T": "-",
        "VPNv6\nR/A/T": "-",
    }
    peer.update(overrides)
    return {"leaf1": [{"NI": "default", "Neighbors": [peer]}]}


def test_bgp_down_finds_a_session_that_is_not_established():
    findings = run("bgp_down", fabric(bgp_peers=_peers(state="active")))
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert findings[0]["Node"] == "leaf1"
    assert findings[0]["Subject"] == "default/10.0.0.2"
    assert "session is active" in findings[0]["Detail"]
    assert "peer-group spines" in findings[0]["Detail"]


def test_bgp_down_leaves_an_established_session_alone():
    assert run("bgp_down", fabric(bgp_peers=_peers())) == []


def test_bgp_down_reads_the_state_whatever_its_case():
    assert run("bgp_down", fabric(bgp_peers=_peers(state="Established"))) == []


def test_bgp_down_does_not_report_a_session_that_is_not_meant_to_be_up():
    """The peers report carries no admin-state to tell a disabled peer by."""
    assert run("bgp_down", fabric(bgp_peers=_peers(state=""))) == []


def test_bgp_af_down_finds_the_family_under_an_established_session():
    peers = _peers(**{"EVPN\nR/A/T": "down"})
    findings = run("bgp_af_down", fabric(bgp_peers=peers))
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert "evpn is down" in findings[0]["Detail"]


def test_bgp_af_down_says_nothing_about_a_session_already_reported_down():
    """One fault is one finding: bgp_down has it."""
    peers = _peers(state="idle", **{"EVPN\nR/A/T": "down"})
    assert run("bgp_af_down", fabric(bgp_peers=peers)) == []


def test_bgp_af_down_ignores_a_family_that_was_never_enabled():
    assert run("bgp_af_down", fabric(bgp_peers=_peers())) == []


def test_bgp_no_routes_finds_a_family_that_has_learned_nothing():
    peers = _peers(**{"EVPN\nR/A/T": "0/0/6"})
    findings = run("bgp_no_routes", fabric(bgp_peers=peers))
    assert len(findings) == 1
    assert findings[0]["Severity"] == WARNING
    assert "evpn is up but has received no routes" in findings[0]["Detail"]


def test_bgp_no_routes_counts_received_rather_than_active():
    """A route received and not selected is a policy question, not a fault."""
    peers = _peers(**{"EVPN\nR/A/T": "12/0/6"})
    assert run("bgp_no_routes", fabric(bgp_peers=peers)) == []


def test_bgp_no_routes_ignores_a_family_that_is_not_carrying():
    peers = _peers(**{"EVPN\nR/A/T": "disabled"})
    assert run("bgp_no_routes", fabric(bgp_peers=peers)) == []


def test_bgp_checks_survive_a_network_instance_with_no_neighbors():
    state = fabric(bgp_peers={"leaf1": [{"NI": "default", "Neighbors": None}]})
    assert run("bgp_down", state) == []


# --------------------------------------------------------------------------- #
# interfaces
# --------------------------------------------------------------------------- #


def _subif(node: str = "leaf1", **overrides: Any) -> Dict[str, Any]:
    subif = {
        "Subitf": "ethernet-1/1.0",
        "type": "routed",
        "admin": "enable",
        "oper": "up",
        "down-reason": "",
        "ip-mtu": 9000,
    }
    subif.update(overrides)
    return {node: [{"Itf": "ethernet-1/1", "subitfs": [subif]}]}


def test_itf_down_finds_a_subinterface_enabled_but_not_up():
    state = fabric(subif=_subif(oper="down", **{"down-reason": "port-down"}))
    findings = run("itf_down", state)
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert findings[0]["Subject"] == "ethernet-1/1.0"
    assert "port-down" in findings[0]["Detail"]


def test_itf_down_leaves_a_port_held_down_on_purpose_alone():
    """The standby side of a single-active segment reads as down/standby."""
    state = fabric(subif=_subif(oper="down/standby", **{"down-reason": "standby-signaling"}))
    assert run("itf_down", state) == []


def test_itf_down_leaves_an_administratively_disabled_port_alone():
    state = fabric(subif=_subif(oper="down", admin="disable"))
    assert run("itf_down", state) == []


def test_itf_down_says_so_when_the_node_gave_no_reason():
    findings = run("itf_down", fabric(subif=_subif(oper="down")))
    assert "no reason reported" in findings[0]["Detail"]


def test_itf_down_ignores_the_management_interface():
    state = fabric(
        subif={
            "leaf1": [
                {"Itf": "mgmt0", "subitfs": [{"Subitf": "mgmt0.0", "admin": "enable", "oper": "down"}]}
            ]
        }
    )
    assert run("itf_down", state) == []


def _ifstats(**overrides: Any) -> Dict[str, Any]:
    row = {
        "interface": "ethernet-1/1",
        "in-Kbps": 12.0,
        "out-Kbps": 8.0,
        "in-err": 0,
        "out-err": 0,
        "in-disc": 0,
        "out-disc": 0,
    }
    row.update(overrides)
    return {"leaf1": [row]}


def test_itf_errors_finds_error_packets():
    findings = run("itf_errors", fabric(ifstats=_ifstats(**{"in-err": 4})))
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert findings[0]["Subject"] == "ethernet-1/1"
    assert "4 in / 0 out error packets" in findings[0]["Detail"]


def test_itf_errors_reports_discards_less_urgently_than_errors():
    findings = run("itf_errors", fabric(ifstats=_ifstats(**{"out-disc": 7})))
    assert [f["Severity"] for f in findings] == [WARNING]
    assert "0 in / 7 out discarded packets" in findings[0]["Detail"]


def test_itf_errors_reports_errors_and_discards_separately():
    state = fabric(ifstats=_ifstats(**{"in-err": 1, "in-disc": 2}))
    assert sorted(f["Severity"] for f in run("itf_errors", state)) == [ERROR, WARNING]


def test_itf_errors_says_nothing_about_a_clean_interface():
    assert run("itf_errors", fabric(ifstats=_ifstats())) == []


# --------------------------------------------------------------------------- #
# topology
# --------------------------------------------------------------------------- #


def _lldp(**adjacencies: List[Dict[str, str]]) -> Dict[str, Any]:
    """``_lldp(leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")])`` per node."""
    return {
        node: [
            {
                "interface": local,
                "Neighbors": [{"Nbr-System": peer, "Nbr-port": peer_port}],
            }
            for local, peer, peer_port in links
        ]
        for node, links in adjacencies.items()
    }


def test_lldp_one_sided_says_nothing_about_a_link_both_ends_see():
    state = fabric(
        lldp=_lldp(
            leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")],
            spine1=[("ethernet-1/1", "leaf1", "ethernet-1/1")],
        )
    )
    assert run("lldp_one_sided", state) == []


def test_lldp_one_sided_finds_the_end_that_is_not_seen_back():
    state = fabric(
        lldp=_lldp(
            leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")],
            spine1=[],
        )
    )
    findings = run("lldp_one_sided", state)
    assert len(findings) == 1
    assert findings[0]["Node"] == "leaf1"
    assert findings[0]["Subject"] == "ethernet-1/1"
    assert "does not see it back" in findings[0]["Detail"]


def test_lldp_one_sided_ignores_a_neighbour_that_is_not_in_the_inventory():
    """A node we do not poll cannot be expected to report anything back."""
    state = fabric(lldp=_lldp(leaf1=[("ethernet-1/1", "some-router", "xe-0/0/0")]))
    assert run("lldp_one_sided", state) == []


def test_lldp_one_sided_matches_a_short_system_name_to_a_prefixed_inventory():
    """containerlab names a host clab-dc1-leaf1; the node advertises leaf1."""
    state = FabricState(
        reports={
            "lldp": {
                "clab-dc1-leaf1": [
                    {
                        "interface": "ethernet-1/1",
                        "Neighbors": [{"Nbr-System": "spine1", "Nbr-port": "ethernet-1/1"}],
                    }
                ],
                "clab-dc1-spine1": [
                    {
                        "interface": "ethernet-1/1",
                        "Neighbors": [{"Nbr-System": "leaf1", "Nbr-port": "ethernet-1/1"}],
                    }
                ],
            }
        }
    )
    assert run("lldp_one_sided", state) == []


def test_lldp_one_sided_ignores_management_links():
    state = fabric(lldp=_lldp(leaf1=[("mgmt0", "spine1", "mgmt0")], spine1=[]))
    assert run("lldp_one_sided", state) == []


def _link_pair() -> Dict[str, Any]:
    return _lldp(
        leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")],
        spine1=[("ethernet-1/1", "leaf1", "ethernet-1/1")],
    )


def _mtu(node: str, mtu: int, index: str = "0") -> Dict[str, Any]:
    return {
        node: [
            {
                "Itf": "ethernet-1/1",
                "subitfs": [{"Subitf": f"ethernet-1/1.{index}", "admin": "enable", "oper": "up", "ip-mtu": mtu}],
            }
        ]
    }


def test_mtu_mismatch_finds_two_ends_that_disagree():
    state = fabric(
        lldp=_link_pair(),
        subif={**_mtu("leaf1", 9000), **_mtu("spine1", 1500)},
    )
    findings = run("mtu_mismatch", state)
    assert len(findings) == 1
    assert findings[0]["Severity"] == ERROR
    assert findings[0]["Subject"] == "ethernet-1/1.0"
    assert "9000" in findings[0]["Detail"] and "1500" in findings[0]["Detail"]


def test_mtu_mismatch_reports_a_link_once_though_both_ends_see_it():
    state = fabric(
        lldp=_link_pair(),
        subif={**_mtu("leaf1", 9000), **_mtu("spine1", 1500)},
    )
    assert len(run("mtu_mismatch", state)) == 1


def test_mtu_mismatch_says_nothing_when_the_ends_agree():
    state = fabric(
        lldp=_link_pair(),
        subif={**_mtu("leaf1", 9000), **_mtu("spine1", 9000)},
    )
    assert run("mtu_mismatch", state) == []


def test_mtu_mismatch_compares_subinterfaces_of_the_same_index():
    """Two services on one trunk are two MTUs, and neither is the other's."""
    state = fabric(
        lldp=_link_pair(),
        subif={**_mtu("leaf1", 9000, index="0"), **_mtu("spine1", 1500, index="10")},
    )
    assert run("mtu_mismatch", state) == []


def test_mtu_mismatch_needs_both_ends_to_report_one():
    state = fabric(lldp=_link_pair(), subif=_mtu("leaf1", 9000))
    assert run("mtu_mismatch", state) == []


# --------------------------------------------------------------------------- #
# EVPN services
# --------------------------------------------------------------------------- #


def _ni(node: str, *, vxlan: str = "vxlan1.100", in_rt: str = "65000:100", out_rt: str = "65000:100"):
    return {
        node: [
            {
                "NI": "mac-vrf-100",
                "type": "mac-vrf",
                "oper": "up",
                "vxlan-itf": vxlan,
                "In-RT": in_rt,
                "Out-RT": out_rt,
            }
        ]
    }


def _vxlan(node: str, *, itf: str = "vxlan1.100", vni: int = 100):
    return {node: [{"vxlan-itf": itf, "NI": "mac-vrf-100", "ing-vni": vni}]}


def test_evpn_service_mismatch_says_nothing_when_two_nodes_agree():
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2")},
    )
    assert run("evpn_service_mismatch", state) == []


def test_evpn_service_mismatch_finds_a_vni_that_differs():
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2")},
        vxlan={**_vxlan("leaf1", vni=100), **_vxlan("leaf2", vni=200)},
    )
    findings = run("evpn_service_mismatch", state)
    # Both ends are wrong until someone decides which one is right.
    assert {f["Node"] for f in findings} == {"leaf1", "leaf2"}
    assert all(f["Subject"] == "mac-vrf-100" for f in findings)
    assert all("VNI" in f["Detail"] for f in findings)


def test_evpn_service_mismatch_finds_route_targets_that_differ():
    state = fabric(
        ni={**_ni("leaf1"), **_ni("leaf2", out_rt="65000:999")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2")},
    )
    findings = run("evpn_service_mismatch", state)
    assert findings and all("export route-target" in f["Detail"] for f in findings)


def test_evpn_service_mismatch_reads_a_route_target_however_it_is_written():
    """One report strips the 'target:' prefix and another keeps it."""
    state = fabric(
        ni={**_ni("leaf1", in_rt="65000:100"), **_ni("leaf2", in_rt="target:65000:100")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2")},
    )
    assert run("evpn_service_mismatch", state) == []


def test_evpn_service_mismatch_ignores_a_service_only_one_node_has():
    """A service on one leaf is a service, not a disagreement."""
    state = fabric(ni=_ni("leaf1"), vxlan=_vxlan("leaf1"))
    assert run("evpn_service_mismatch", state) == []


def test_evpn_service_mismatch_ignores_the_default_network_instance():
    state = fabric(
        ni={
            "leaf1": [{"NI": "default", "type": "default", "In-RT": "", "Out-RT": ""}],
            "leaf2": [{"NI": "default", "type": "default", "In-RT": "x", "Out-RT": "y"}],
        },
        vxlan={},
    )
    assert run("evpn_service_mismatch", state) == []


# --------------------------------------------------------------------------- #
# ethernet segments
# --------------------------------------------------------------------------- #


def _es(node: str, **overrides: Any) -> Dict[str, Any]:
    segment = {
        "name": "es-1",
        "esi": "01:00:00:00:00:01:00:00:00:01",
        "type": "virtual",
        "mh-mode": "all-active",
        "oper": "up",
        "itf/nh": "lag1",
        "evi": "100",
        "ni-peers": "mac-vrf-100:[10.0.0.1 10.0.0.2(DF)]",
    }
    segment.update(overrides)
    return {node: [segment]}


def test_es_df_says_nothing_about_a_healthy_segment():
    assert run("es_df", fabric(es={**_es("leaf1"), **_es("leaf2")})) == []


def test_es_df_finds_a_network_instance_with_no_designated_forwarder():
    state = fabric(es=_es("leaf1", **{"ni-peers": "mac-vrf-100:[10.0.0.1 10.0.0.2]"}))
    findings = run("es_df", state)
    assert len(findings) == 1
    assert findings[0]["Subject"] == "es-1/mac-vrf-100"
    assert "no designated forwarder" in findings[0]["Detail"]


def test_es_df_finds_a_network_instance_with_no_candidates_at_all():
    state = fabric(es=_es("leaf1", **{"ni-peers": "mac-vrf-100:[]"}))
    findings = run("es_df", state)
    assert len(findings) == 1
    assert "no candidates" in findings[0]["Detail"]


def test_es_df_checks_every_network_instance_on_a_segment():
    state = fabric(
        es=_es(
            "leaf1",
            **{"ni-peers": "mac-vrf-100:[10.0.0.1(DF)], mac-vrf-200:[10.0.0.1 10.0.0.2]"},
        )
    )
    findings = run("es_df", state)
    assert [f["Subject"] for f in findings] == ["es-1/mac-vrf-200"]


def test_es_df_finds_a_segment_that_is_down():
    findings = run("es_df", fabric(es=_es("leaf1", oper="down")))
    assert any("segment is down" in f["Detail"] for f in findings)


def test_es_df_finds_two_nodes_disagreeing_about_the_multi_homing_mode():
    state = fabric(es={**_es("leaf1"), **_es("leaf2", **{"mh-mode": "single-active"})})
    findings = run("es_df", state)
    assert {f["Node"] for f in findings} == {"leaf1", "leaf2"}
    assert all("multi-homing mode" in f["Detail"] for f in findings)


def test_es_df_does_not_compare_two_different_segments():
    state = fabric(
        es={
            **_es("leaf1"),
            **_es("leaf2", esi="01:00:00:00:00:02:00:00:00:02", **{"mh-mode": "single-active"}),
        }
    )
    assert run("es_df", state) == []


# --------------------------------------------------------------------------- #
# running them together
# --------------------------------------------------------------------------- #


def test_a_healthy_fabric_produces_nothing():
    state = fabric(
        bgp_peers=_peers(),
        subif={**_mtu("leaf1", 9000), **_mtu("spine1", 9000)},
        ifstats=_ifstats(),
        lldp=_link_pair(),
        ni={**_ni("leaf1"), **_ni("leaf2")},
        vxlan={**_vxlan("leaf1"), **_vxlan("leaf2")},
        es={**_es("leaf1"), **_es("leaf2")},
    )
    assert run_checks(state) == []


def test_findings_come_back_worst_first():
    state = fabric(
        bgp_peers=_peers(state="idle"),
        ifstats=_ifstats(**{"in-disc": 3}),
    )
    severities = [f.severity for f in run_checks(state)]
    assert severities == [ERROR, WARNING]


def test_only_runs_the_checks_asked_for():
    state = fabric(bgp_peers=_peers(state="idle"), ifstats=_ifstats(**{"in-err": 1}))
    findings = run_checks(state, only=["bgp_down"])
    assert {f.check for f in findings} == {"bgp_down"}


def test_a_check_whose_reports_were_not_collected_is_skipped():
    """Not asking a question is not the same as getting a clean answer."""
    findings = run_checks(fabric(bgp_peers=_peers(state="idle")))
    assert {f.check for f in findings} == {"bgp_down"}


def test_a_node_that_could_not_be_read_is_reported_rather_than_passed_over():
    state = fabric(bgp_peers=_peers())
    state.errors[("bgp_peers", "leaf9")] = "not connected"
    findings = run_checks(state)
    assert len(findings) == 1
    assert findings[0].check == "collection"
    assert findings[0].node == "leaf9"
    assert findings[0].severity == WARNING
    assert "not connected" in findings[0].detail


def test_a_check_that_raises_is_a_finding_rather_than_a_crash(monkeypatch):
    """One bad check must not take the whole report down with it."""

    def explode(_state):
        raise RuntimeError("boom")

    broken = Check(
        name="broken", title="Broken", requires=("bgp_peers",), run=explode
    )
    monkeypatch.setattr(checks_module, "CHECKS", (broken,))
    findings = run_checks(fabric(bgp_peers=_peers()))
    assert len(findings) == 1
    assert findings[0].check == "broken"
    assert findings[0].severity == ERROR
    assert "boom" in findings[0].detail


def test_checks_tolerate_a_report_that_came_back_empty():
    state = fabric(bgp_peers={"leaf1": []}, subif={"leaf1": []}, ifstats={"leaf1": []})
    assert run_checks(state) == []
