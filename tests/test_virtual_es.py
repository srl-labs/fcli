"""A virtual ethernet-segment on the topology: L3 aliasing in a routed service.

Shaped after the 3-stage NVD lab: host6 is multi-homed to leaf5 and leaf6 in
macvrf-104, whose IRB is in ipvrf-1; a virtual segment on the leaves tracks
host6's address as its next-hop, so the remote leaves load-balance the
prefixes host6 advertises over both.
"""

from typing import Any, Dict, List

from nornir_srl.fabric import FabricState
from nornir_srl.records import Route, RouteNextHop, RouteTable
from nornir_srl.server.topology import annotate_aliasing, build_topology, node_facts

ESI = "00:01:ff:ff:ff:ff:ff:ff:ff:06"


def _ves(df: str) -> Dict[str, Any]:
    return {
        "name": "vES-host6-tenant1",
        "type": "virtual",
        "esi": ESI,
        "oper-state": "up",
        "multi-homing-mode": "single-active",
        "next-hop": [{"l3-next-hop": "10.1.4.16", "evi": [{"start": 1}]}],
        "association": {
            "network-instance": [
                {
                    "name": "ipvrf-1",
                    "bgp-instance": [
                        {
                            "computed-designated-forwarder-candidates": {
                                "designated-forwarder-candidate": [
                                    {"address": "192.0.2.15", "designated-forwarder": df == "192.0.2.15"},
                                    {"address": "192.0.2.16", "designated-forwarder": df == "192.0.2.16"},
                                ]
                            }
                        }
                    ],
                }
            ]
        },
    }


def _leaf(name: str, system: str, df: str = "", attached: bool = True, host: bool = False) -> Any:
    interfaces: List[Dict[str, Any]] = [
        {"name": "system0", "subinterface": [{"index": 0, "ipv4": {"address": [{"ip-prefix": f"{system}/32"}]}}]},
        {"name": "irb0", "subinterface": [{"index": 104, "ipv4": {"address": [{"ip-prefix": "10.1.4.254/24"}]}}]},
    ]
    instances: List[Dict[str, Any]] = [
        {"name": "ipvrf-1", "type": "ip-vrf", "interface": [{"name": "irb0.104"}]},
        {"name": "macvrf-104", "type": "mac-vrf", "interface": [{"name": "irb0.104"}]},
    ]
    if host:
        interfaces.append({"name": "lag1", "oper-state": "up", "subinterface": [{"index": 104}]})
        instances[1]["interface"].append({"name": "lag1.104"})
    system_tree: Dict[str, Any] = {"name": {"host-name": name}, "lldp": {"interface": []}}
    if df or not attached:
        system_tree["network-instance"] = {
            "protocols": {"evpn": {"ethernet-segments": {"bgp-instance": [{"id": 1, "ethernet-segment": [_ves(df or "192.0.2.15")]}]}}}
        }
    return node_facts(name, snapshot={"system": system_tree, "interface": interfaces, "network-instance": instances})


def _graph(df6: str = "192.0.2.15"):
    return build_topology(
        [
            _leaf("leaf1", "192.0.2.11", attached=False),
            _leaf("leaf3", "192.0.2.13"),
            _leaf("leaf5", "192.0.2.15", df="192.0.2.15", host=True),
            _leaf("leaf6", "192.0.2.16", df=df6, host=True),
        ]
    )


def _ves_node(graph):
    return next(n for n in graph["nodes"] if n.get("virtual"))


def test_the_segment_is_a_node_serving_its_routed_service():
    node = _ves_node(_graph())
    assert node["name"] == f"ves:{ESI}" and node["label"] == "vES"
    assert node["services"] == ["ipvrf-1"] and node["overlay_only"] == ["ipvrf-1"]
    ves = node["ves"]
    assert ves["next_hops"] == [{"address": "10.1.4.16", "evis": ["1"], "via": "macvrf-104"}]
    # attached is what the DF election names; leaf1 only has it configured
    assert ves["attached"] == ["leaf5", "leaf6"]
    assert ves["configured"] == ["leaf1", "leaf5", "leaf6"]
    assert ves["df"] == {"ipvrf-1": ["leaf5"]} and ves["df_conflict"] == []


def test_it_is_cabled_to_its_leaves_and_to_the_client_owning_the_next_hop():
    graph = _graph()
    links = {(l["a"], l["b"]): l for l in graph["links"] if l.get("kind")}
    ves = f"ves:{ESI}"
    to_leaf5 = links.get(("leaf5", ves)) or links.get((ves, "leaf5"))
    assert to_leaf5["kind"] == "ves" and to_leaf5["df"] and to_leaf5["overlay_only"] == ["ipvrf-1"]
    assert not (links.get(("leaf6", ves)) or links.get((ves, "leaf6"))).get("df")
    # host6's lag carries no ESI here, so each leaf's port is a client of its own
    owners = [l for l in links.values() if l["kind"] == "ves-nh"]
    assert {l["a"] for l in owners} == {"leaf5:lag1", "leaf6:lag1"}
    assert {l["note"] for l in owners} == {"next-hop 10.1.4.16 via macvrf-104"}


def test_two_leaves_that_each_elect_themselves_are_a_conflict():
    ves = _ves_node(_graph(df6="192.0.2.16"))["ves"]
    assert ves["df"] == {"ipvrf-1": ["leaf5", "leaf6"]}
    assert ves["df_conflict"] == ["ipvrf-1"]


def test_a_remote_vtep_that_spreads_the_next_hop_over_both_leaves_is_aliasing():
    graph = _graph()
    evpn = lambda *vteps: tuple(RouteNextHop(address=v, type="indirect") for v in vteps)  # noqa: E731
    table = RouteTable(
        "ipvrf-1",
        (
            Route("10.1.4.16/32", "bgp-evpn-ifl-host", next_hops=evpn("192.0.2.15", "192.0.2.16")),
            Route("6.6.6.1/32", "bgp", next_hops=(RouteNextHop(type="indirect", resolving_route="10.1.4.16/32"),)),
        ),
    )
    one_way = RouteTable("ipvrf-1", (Route("10.1.4.16/32", "bgp-evpn", next_hops=evpn("192.0.2.15")),))
    state = FabricState(reports={"ipv4_rib": {"leaf1": [table], "leaf3": [one_way]}})
    annotate_aliasing(graph, state)
    (alias,) = _ves_node(graph)["ves"]["aliasing"]
    assert alias == {"node": "leaf1", "ni": "ipvrf-1", "vteps": ["leaf5", "leaf6"], "prefixes": ["6.6.6.1/32"]}
    (link,) = [l for l in graph["links"] if l.get("kind") == "alias"]
    assert link["a"] == "leaf1" and "6.6.6.1/32" in link["note"]


def test_a_fabric_without_virtual_segments_draws_none():
    graph = build_topology([node_facts("leaf1", snapshot={"system": {"name": {"host-name": "leaf1"}}})])
    assert not [n for n in graph["nodes"] if n.get("virtual")]


def test_a_segment_s_findings_are_drawn_on_its_node():
    from nornir_srl.checks import Finding
    from nornir_srl.server.topology import annotate_health

    graph = _graph(df6="192.0.2.16")
    finding = Finding("es_df", "error", "leaf6", "vES-host6-tenant1/ipvrf-1", "nodes disagree on the designated forwarder")
    annotate_health(graph, [(finding, None)], [])
    node = _ves_node(graph)
    assert node["findings"]["error"] == 1 and node["health"] == "error"
    assert node["issues"][0]["check"] == "es_df"
