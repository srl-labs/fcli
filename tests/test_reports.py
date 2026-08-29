"""Unit tests for report helpers and getters added for DCI use-cases.

These tests use small in-memory fixtures and a fake gNMI ``get`` so they run
without a live device.
"""

from typing import Any, Dict, List, Optional

from nornir_srl.connections.helpers import clean_structured_key
from nornir_srl.connections.interfaces import NetworkInstanceMixin
from nornir_srl.connections.routing import RoutingMixin

# --------------------------------------------------------------------------- #
# clean_structured_key
# --------------------------------------------------------------------------- #


def test_clean_structured_key_strips_order_prefix():
    assert clean_structured_key("0_st") == "st"
    assert clean_structured_key("1_peer") == "peer"
    assert clean_structured_key("10_foo") == "foo"


def test_clean_structured_key_collapses_newlines():
    assert clean_structured_key("AF: EVPN\nRx/Act/Tx") == "AF: EVPN Rx/Act/Tx"
    assert clean_structured_key("U4 R/A/T") == "U4 R/A/T"
    assert clean_structured_key("U4\nR/A/T") == "U4 R/A/T"
    assert clean_structured_key("EVPN\nR/A/T") == "EVPN R/A/T"


def test_clean_structured_key_leaves_plain_keys():
    assert clean_structured_key("Node") == "Node"
    assert clean_structured_key("next-hop") == "next-hop"


def test_clean_structured_key_passthrough_non_str():
    assert clean_structured_key(5) == 5
    assert clean_structured_key(None) is None


# --------------------------------------------------------------------------- #
# Fake device wiring
# --------------------------------------------------------------------------- #


class _FakeRouting(RoutingMixin):
    """RoutingMixin with a scripted ``get`` keyed on path substrings."""

    def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
        self._responses = responses
        self.capabilities = {
            "supported_models": [{"name": "bgp-rib", "version": "2024-10-31"}]
        }

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        path = paths[0]
        for key, resp in self._responses.items():
            if key in path:
                return resp
        raise KeyError(f"no scripted response for path {path}")


class _FakeInterfaces(NetworkInstanceMixin):
    """NetworkInstanceMixin with a scripted ``get`` keyed on path substrings."""

    def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
        self._responses = responses

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        path = paths[0]
        for key, resp in self._responses.items():
            if key in path:
                return resp
        raise KeyError(f"no scripted response for path {path}")


# --------------------------------------------------------------------------- #
# get_bgp_rib path attributes (detail=True)
# --------------------------------------------------------------------------- #


def test_get_bgp_rib_evpn_detail_attributes():
    attr_sets = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "attr-sets": {
                            "attr-set": [
                                {
                                    "index": 1,
                                    "origin": "igp",
                                    "as-path": {"segment": [{"member": [65000]}]},
                                    "communities": {
                                        "community": ["65000:1"],
                                        "ext-community": [
                                            "target:65000:100",
                                            "origin:65000:1",
                                            "bgp-tunnel-encap:MPLS",
                                        ],
                                    },
                                    "domain-path": {
                                        "domain-segment": [
                                            {"domain": {"domain-id": ["65000:1"]}}
                                        ]
                                    },
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]
    routes = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "afi-safi": [
                            {
                                "evpn": {
                                    "rib-in-out": {
                                        "rib-in-post": {
                                            "mac-ip-route": [
                                                {
                                                    "attr-id": 1,
                                                    "used-route": True,
                                                    "valid-route": True,
                                                    "best-route": True,
                                                    "neighbor": "192.0.2.2",
                                                    "neighbor-as": 65002,
                                                    "tie-break-reason": "none",
                                                    "internal-tags": [
                                                        "tag-value = 0x1"
                                                    ],
                                                    "route-distinguisher": "192.0.2.2:100",
                                                    "esi": "00:00:00:00:00:00:00:00:00:00",
                                                    "mac-address": "1A:DC:0E:FF:00:41",
                                                    "ip-address": "10.0.0.1",
                                                    "next-hop": "192.0.2.2",
                                                    "label": {"value": 100},
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    },
                }
            ]
        }
    ]

    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "mac-ip-route": routes})
    out = dev.get_bgp_rib(route_fam="evpn", route_type="2", detail=True)
    rib = out["bgp_rib"]
    assert len(rib) == 1
    route = rib[0]["Rib"][0]
    assert route["RT"] == "65000:100"
    assert route["soo"] == "65000:1"
    assert route["tunnel-encap"] == "MPLS"
    assert route["dpath"] == "65000:1"
    assert route["communities"] == "65000:1"
    assert route["valid"] is True and route["best"] is True and route["used"] is True
    assert route["tie-break"] == "none"
    assert route["internal-tags"] == ["tag-value = 0x1"]
    assert route["neighbor-as"] == 65002


def test_get_bgp_rib_evpn_lean_has_no_extra_attrs():
    """Without detail, the lean projection must not include the extra fields."""
    attr_sets = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {"attr-sets": {"attr-set": [{"index": 1}]}},
                }
            ]
        }
    ]
    routes = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "afi-safi": [
                            {
                                "evpn": {
                                    "rib-in-out": {
                                        "rib-in-post": {
                                            "mac-ip-route": [
                                                {
                                                    "attr-id": 1,
                                                    "used-route": True,
                                                    "valid-route": True,
                                                    "best-route": True,
                                                    "neighbor": "192.0.2.2",
                                                    "route-distinguisher": "192.0.2.2:100",
                                                    "esi": "00:00:00:00:00:00:00:00:00:00",
                                                    "mac-address": "1A:DC:0E:FF:00:41",
                                                    "ip-address": "10.0.0.1",
                                                    "next-hop": "192.0.2.2",
                                                    "label": {"value": 100},
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    },
                }
            ]
        }
    ]
    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "mac-ip-route": routes})
    out = dev.get_bgp_rib(route_fam="evpn", route_type="2", detail=False)
    route = out["bgp_rib"][0]["Rib"][0]
    assert "soo" not in route
    assert "dpath" not in route
    assert "communities" not in route


def test_get_bgp_rib_l3vpn_ipv4_alias_and_columns():
    """L3VPN IPv4 RIB uses RD + Pfx; ``l3vpn-v4`` is an accepted alias."""
    attr_sets = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "attr-sets": {
                            "attr-set": [
                                {
                                    "index": 1,
                                    "as-path": {"segment": [{"member": [65002, "i"]}]},
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]
    routes = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {
                        "afi-safi": [
                            {
                                "afi-safi-name": "l3vpn-ipv4-unicast",
                                "l3vpn-ipv4-unicast": {
                                    "local-rib": {
                                        "route": [
                                            {
                                                "attr-id": 1,
                                                "used-route": True,
                                                "valid-route": True,
                                                "best-route": True,
                                                "neighbor": "10.0.0.6",
                                                "route-distinguisher": "65000:1",
                                                "ipv4-prefix": "172.16.1.0/24",
                                                "next-hop": "10.0.0.6",
                                                "local-pref": 100,
                                                "med": 0,
                                                "communities": {
                                                    "community": [],
                                                    "large-community": [],
                                                },
                                            }
                                        ]
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        }
    ]
    dev = _FakeRouting({"attr-sets/attr-set": attr_sets, "local-rib/route": routes})
    out = dev.get_bgp_rib(route_fam="l3vpn-v4", detail=False)
    route = out["bgp_rib"][0]["Rib"][0]
    assert route["RD"] == "65000:1"
    assert route["Pfx"] == "172.16.1.0/24"
    assert route["neighbor"] == "10.0.0.6"
    assert route["0_st"] == "u*>"


def test_get_bgp_rib_l3vpn_returns_empty_when_rib_path_absent():
    """Nodes without an L3VPN RIB path (e.g. EVPN-only leaves) return an empty RIB."""

    import grpc

    class _RpcNotFound(Exception):
        def code(self) -> grpc.StatusCode:
            return grpc.StatusCode.NOT_FOUND

    class _LeafNoL3vpn(_FakeRouting):
        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            p = paths[0]
            if "l3vpn-ipv4-unicast" in p and "local-rib" in p:
                raise _RpcNotFound()
            return super().get(paths, datatype, strip_mod)

    attr_only = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {"attr-sets": {"attr-set": []}},
                }
            ]
        }
    ]
    dev = _LeafNoL3vpn({"attr-sets/attr-set": attr_only})
    assert dev.get_bgp_rib(route_fam="l3vpn-v4") == {"bgp_rib": []}


def test_get_bgp_rib_l3vpn_empty_on_pygnmi_path_invalid_message():
    """pygnmi surfaces SR Linux path errors as a string (no grpc __cause__ chain)."""

    class _LeafNoL3vpnPygnmi(_FakeRouting):
        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            p = paths[0]
            if "l3vpn-ipv4-unicast" in p and "local-rib" in p:
                raise RuntimeError(
                    "GRPC ERROR Host: leaf2:57400, Error: Path not valid - unknown element "
                    "'l3vpn-ipv4-unicast'. Options are [ipv4-unicast, ipv6-unicast, evpn, "
                    "ipv4-flowspec-v1, ipv6-flowspec-v1, route-target, afi-safi-name]"
                )
            return super().get(paths, datatype, strip_mod)

    attr_only = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {"attr-sets": {"attr-set": []}},
                }
            ]
        }
    ]
    dev = _LeafNoL3vpnPygnmi({"attr-sets/attr-set": attr_only})
    assert dev.get_bgp_rib(route_fam="l3vpn-v4") == {"bgp_rib": []}


def test_get_bgp_rib_l3vpn_empty_when_orig_exc_has_grpc_code():
    """pygnmi ``gNMIException`` stores RpcError in ``orig_exc``, not ``__cause__``."""

    import grpc

    class _InactiveLike:
        def code(self) -> grpc.StatusCode:
            return grpc.StatusCode.INVALID_ARGUMENT

    class _GnmiExcWrapper(Exception):
        def __init__(self) -> None:
            super().__init__("GRPC ERROR Host: leaf:57400, Error: Path not valid")
            self.orig_exc = _InactiveLike()

    class _LeafOrig(_FakeRouting):
        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            p = paths[0]
            if "l3vpn-ipv4-unicast" in p and "local-rib" in p:
                raise _GnmiExcWrapper()
            return super().get(paths, datatype, strip_mod)

    attr_only = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "bgp-rib": {"attr-sets": {"attr-set": []}},
                }
            ]
        }
    ]
    dev = _LeafOrig({"attr-sets/attr-set": attr_only})
    assert dev.get_bgp_rib(route_fam="l3vpn-v4") == {"bgp_rib": []}


# --------------------------------------------------------------------------- #
# get_tunnel_table next-hop resolution
# --------------------------------------------------------------------------- #


def test_get_tunnel_table_resolves_egress_and_label():
    next_hops = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "route-table": {
                        "next-hop": [
                            {
                                "index": "10",
                                "type": "mpls",
                                "ip-address": "10.255.0.1",
                                "subinterface": "ethernet-1/5.0",
                                "mpls-encapsulation": {
                                    "pushed-mpls-label-stack": [20000]
                                },
                            }
                        ]
                    },
                }
            ]
        }
    ]
    next_hop_groups = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "route-table": {
                        "next-hop-group": [
                            {"index": "77", "next-hop": [{"next-hop": "10"}]}
                        ]
                    },
                }
            ]
        }
    ]
    tunnel_table = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "tunnel-table": {
                        "ipv4": {
                            "tunnel": [
                                {
                                    "ipv4-prefix": "192.0.2.152/32",
                                    "type": "ldp",
                                    "owner": "ldp_mgr",
                                    "id": 65537,
                                    "next-hop-group": "77",
                                    "metric": 10,
                                    "preference": 9,
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]

    dev = _FakeRouting(
        {
            "next-hop-group[index=*]": next_hop_groups,
            "next-hop[index=*]": next_hops,
            "tunnel-table": tunnel_table,
        }
    )
    out = dev.get_tunnel_table()
    rows = out["tunnel_table"]
    assert len(rows) == 1
    row = rows[0]
    assert row["Prefix"] == "192.0.2.152/32"
    assert row["type"] == "ldp"
    assert row["pref"] == 9
    assert row["metric"] == 10
    assert row["next-hop"] == ["10.255.0.1"]
    assert row["egress-itf"] == ["ethernet-1/5.0"]
    assert row["label"] == ["20000"]


def test_get_bridge_domains():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    data = [
        {
            "network-instance": [
                {
                    "name": "default",
                    "type": "default",
                    "oper-state": "up",
                },
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb1.100"}],
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:999"}],
                                    }
                                }
                            ]
                        }
                    },
                },
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:100"}],
                                        "export-rt": [{"target": "target:65000:100"}],
                                    }
                                }
                            ]
                        }
                    },
                    "interface": [
                        {"name": "ethernet-1/1.100"},
                        {"name": "ethernet-1/2.100"},
                        {
                            "name": "irb1.100",
                            "ipv4": {
                                "address": [
                                    {"ip-prefix": "10.1.100.1/24", "anycast-gw": True}
                                ]
                            },
                        },
                    ],
                    "vxlan-interface": [{"name": "vxlan1.100"}],
                },
            ]
        }
    ]

    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    out = dev.get_bridge_domains()
    bds = out["bridge_domains"]
    assert len(bds) == 1
    bd = bds[0]
    assert bd["Bridge Domain"] == "target:65000:100"
    assert bd["MAC-VRF"] == "mac-vrf-100"
    assert bd["Oper State"] == "up"
    assert bd["Subnets"] == "10.1.100.0/24"
    assert bd["IRB Interface"] == "irb1.100 [up]: 10.1.100.1/24 (anycast-gw: true) -> ip-vrf-1"
    assert bd["Sub-Interfaces"] == "ethernet-1/1.100 [up] (VLAN: 100), ethernet-1/2.100 [up] (VLAN: 100)"
    assert bd["VXLAN Interface"] == "vxlan1.100"
    assert bd["Gateway"] == ""
    assert bd["BGP Instance"] == ""
    assert bd["System IPv4"] == ""
    assert bd["System IPv6"] == ""


def test_system0_addresses_skips_link_local_and_reads_system0():
    from nornir_srl.connections.layer2 import _system0_addresses

    assert _system0_addresses({}) == ("", "")
    ipv4, ipv6 = _system0_addresses(
        {
            "system0.0": {
                "ipv4": {"address": [{"ip-prefix": "192.0.2.11/32"}]},
                "ipv6": {
                    "address": [
                        {"ip-prefix": "fe80::1/64"},
                        {"ip-prefix": "2001:db8::11/128"},
                    ]
                },
            }
        }
    )
    assert ipv4 == "192.0.2.11"
    assert ipv6 == "2001:db8::11"


def test_get_bridge_domains_includes_system0_addresses():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    network_instances = [
        {
            "network-instance": [
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "ethernet-1/1.100"}],
                }
            ]
        }
    ]
    subinterfaces = [
        {
            "interface": [
                {
                    "name": "system0",
                    "subinterface": [
                        {
                            "index": 0,
                            "ipv4": {"address": [{"ip-prefix": "10.0.0.1/32"}]},
                            "ipv6": {"address": [{"ip-prefix": "2001:db8::1/128"}]},
                        }
                    ],
                },
                {
                    "name": "ethernet-1/1",
                    "subinterface": [{"index": 100, "oper-state": "up"}],
                },
            ]
        }
    ]
    dev = _FakeLayer2(
        {"network-instance": network_instances, "subinterface": subinterfaces}
    )
    bd = dev.get_bridge_domains()["bridge_domains"][0]
    assert bd["System IPv4"] == "10.0.0.1"
    assert bd["System IPv6"] == "2001:db8::1"


def test_get_bridge_domains_reports_a_disabled_mac_vrf_from_its_own_view():
    """An IRB held down by its mac-vrf must not read as up.

    SR Linux answers the same question two ways: the subinterface is up under
    ``/interface`` - nothing is wrong with it - while the disabled mac-vrf holding
    it reports it down with ``net-inst-down``. The service listing has to show the
    mac-vrf's view, or the row contradicts itself by pairing a down bridge domain
    with an up IRB.
    """
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    network_instances = [
        {
            "network-instance": [
                {
                    "name": "macvrf-v20",
                    "type": "mac-vrf",
                    "admin-state": "disable",
                    "oper-state": "down",
                    "oper-down-reason": "admin-down",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {"route-target": {"import-rt": [{"target": "target:1:20"}]}}
                            ]
                        }
                    },
                    "interface": [
                        {
                            "name": "irb0.0",
                            "oper-state": "down",
                            "oper-down-reason": "net-inst-down",
                        },
                        {
                            "name": "ethernet-1/3.20",
                            "oper-state": "down",
                            "oper-down-reason": "net-inst-down",
                        },
                    ],
                }
            ]
        }
    ]
    # The /interface view of the very same subinterfaces, which is up.
    subinterfaces = [
        {
            "interface": [
                {
                    "name": "irb0",
                    "subinterface": [
                        {
                            "index": 0,
                            "oper-state": "up",
                            "ipv4": {
                                "address": [
                                    {"ip-prefix": "172.16.20.254/24", "anycast-gw": True}
                                ]
                            },
                        }
                    ],
                },
                {
                    "name": "ethernet-1/3",
                    "subinterface": [{"index": 20, "oper-state": "up"}],
                },
            ]
        }
    ]

    dev = _FakeLayer2(
        {"network-instance": network_instances, "subinterface": subinterfaces}
    )
    bd = dev.get_bridge_domains()["bridge_domains"][0]

    assert bd["Oper State"] == "down"
    assert (
        bd["IRB Interface"]
        == "irb0.0 [down: net-inst-down]: 172.16.20.254/24 (anycast-gw: true)"
    )
    assert bd["Sub-Interfaces"] == "ethernet-1/3.20 [down: net-inst-down] (VLAN: 20)"


def test_get_routers():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    data = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:999"}],
                                        "export-rt": [{"target": "target:65000:999"}],
                                    }
                                }
                            ]
                        }
                    },
                    "interface": [
                        {
                            "name": "irb1.100",
                            "ipv4": {"address": [{"ip-prefix": "10.1.100.1/24"}]},
                            "ipv6": {
                                "address": [
                                    {"ip-prefix": "2001:db8:100::1/64"},
                                    {"ip-prefix": "fe80::1/64"},
                                ]
                            },
                        },
                        {
                            "name": "ethernet-1/10.0",
                            "ipv4": {"address": [{"ip-prefix": "192.168.1.1/30"}]},
                        },
                    ],
                    "vxlan-interface": [{"name": "vxlan1.1"}],
                },
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb1.100"}],
                },
            ]
        }
    ]

    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    out = dev.get_routers()
    routers = out["routers"]
    assert len(routers) == 1
    r = routers[0]
    assert r["Router"] == "target:65000:999"
    assert r["IP-VRF"] == "ip-vrf-1"
    assert r["Oper State"] == "up"
    assert r["Route Targets"] == "target:65000:999"
    assert r["MAC-VRFs"] == (
        "mac-vrf-100 (irb1.100 [up]: 10.1.100.1/24, 2001:db8:100::1/64, fe80::1/64)"
    )
    assert r["Routed Interfaces"] == "ethernet-1/10.0 [up] (192.168.1.1/30)"
    assert r["VXLAN Interface"] == "vxlan1.1"
    assert r["Subnets"] == "10.1.100.0/24, 2001:db8:100::/64"
    assert r["Gateway"] == ""
    assert r["BGP Instance"] == ""

    # Test isolated ip-vrf without route targets
    isolated_data = [
        {
            "network-instance": [
                {
                    "name": "isolated-vrf",
                    "type": "ip-vrf",
                    "oper-state": "up",
                }
            ]
        }
    ]
    dev2 = _FakeLayer2({"network-instance": isolated_data, "subinterface": [{}]})
    out2 = dev2.get_routers()
    r2 = out2["routers"][0]
    assert r2["Route Targets"] == "none (isolated)"
    assert r2["Router"] == "none (isolated) - isolated-vrf"
    assert r2["Gateway"] == ""

    # Test that mgmt ip-vrf is excluded
    mgmt_data = [
        {
            "network-instance": [
                {
                    "name": "mgmt",
                    "type": "ip-vrf",
                    "oper-state": "up",
                }
            ]
        }
    ]
    dev3 = _FakeLayer2({"network-instance": mgmt_data, "subinterface": [{}]})
    out3 = dev3.get_routers()
    assert len(out3["routers"]) == 0


def test_host_ips_from_payload_keeps_only_host_routes():
    from nornir_srl.connections.layer2 import _host_ips_from_payload

    payload = {
        "route": [
            {"ipv4-prefix": "192.0.2.11/32"},
            {"ipv4-prefix": "10.0.0.0/24"},
            {"ipv6-prefix": "2001:db8::1/128"},
            {"ipv6-prefix": "fe80::1/64"},
        ]
    }
    assert _host_ips_from_payload(payload) == ["192.0.2.11", "2001:db8::1"]


def test_assign_underlay_sites_splits_two_dcs_and_ignores_wan_between_gateways():
    """Leaves see only local DCGWs; DCGWs also see the remote DCGWs over the WAN.

    Gateway-to-gateway edges must not merge the two fabrics, or ipvrf-l3dci would
    stay one tile. Each DCGW still joins the DC whose leaves have its system0.
    """
    from nornir_srl.connections.layer2 import assign_underlay_sites

    dc1 = "192.0.2.11 192.0.2.14 192.0.2.151 192.0.2.152"
    dc2 = "192.0.3.15 192.0.3.18 192.0.3.153 192.0.3.154"
    wan = f"{dc1} {dc2}"
    rows = [
        {"Node": "leaf1", "System IPv4": "192.0.2.11", "Underlay Hosts": dc1, "Gateway": ""},
        {"Node": "leaf4", "System IPv4": "192.0.2.14", "Underlay Hosts": dc1, "Gateway": ""},
        {"Node": "dcgw1", "System IPv4": "192.0.2.151", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw2", "System IPv4": "192.0.2.152", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "leaf5", "System IPv4": "192.0.3.15", "Underlay Hosts": dc2, "Gateway": ""},
        {"Node": "leaf8", "System IPv4": "192.0.3.18", "Underlay Hosts": dc2, "Gateway": ""},
        {"Node": "dcgw3", "System IPv4": "192.0.3.153", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw4", "System IPv4": "192.0.3.154", "Underlay Hosts": wan, "Gateway": "Y"},
    ]
    sites = assign_underlay_sites(rows)
    assert set(sites.values()) == {"1", "2"}
    assert {sites["leaf1"], sites["leaf4"], sites["dcgw1"], sites["dcgw2"]} == {"1"}
    assert {sites["leaf5"], sites["leaf8"], sites["dcgw3"], sites["dcgw4"]} == {"2"}
    assert sites["leaf1"] != sites["leaf5"]


def test_assign_underlay_sites_keeps_gateways_together_when_they_all_see_each_other():
    """The WAN/DCI tile is only DCGWs; they share system0 over the WAN, so one Router."""
    from nornir_srl.connections.layer2 import assign_underlay_sites

    wan = "192.0.2.151 192.0.2.152 192.0.3.153 192.0.3.154"
    rows = [
        {"Node": "dcgw1", "System IPv4": "192.0.2.151", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw2", "System IPv4": "192.0.2.152", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw3", "System IPv4": "192.0.3.153", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw4", "System IPv4": "192.0.3.154", "Underlay Hosts": wan, "Gateway": "Y"},
    ]
    assert assign_underlay_sites(rows) == {}


def test_stamp_underlay_sites_splits_dc_tile_but_not_wan_tile():
    from nornir_srl.connections.layer2 import stamp_underlay_sites

    dc1 = "192.0.2.11 192.0.2.151 192.0.2.152"
    dc2 = "192.0.3.15 192.0.3.153 192.0.3.154"
    wan = f"{dc1} {dc2}"
    rows = [
        {"Node": "leaf1", "Router": "target:3000:3000", "System IPv4": "192.0.2.11", "Underlay Hosts": dc1, "Gateway": ""},
        {"Node": "dcgw1", "Router": "target:3000:3000", "System IPv4": "192.0.2.151", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "leaf5", "Router": "target:3000:3000", "System IPv4": "192.0.3.15", "Underlay Hosts": dc2, "Gateway": ""},
        {"Node": "dcgw3", "Router": "target:3000:3000", "System IPv4": "192.0.3.153", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw1", "Router": "target:65000:3000", "System IPv4": "192.0.2.151", "Underlay Hosts": wan, "Gateway": "Y"},
        {"Node": "dcgw3", "Router": "target:65000:3000", "System IPv4": "192.0.3.153", "Underlay Hosts": wan, "Gateway": "Y"},
    ]
    assert stamp_underlay_sites(rows) is True
    dc = [r for r in rows if r["Router"] == "target:3000:3000"]
    wan_rows = [r for r in rows if r["Router"] == "target:65000:3000"]
    assert {r["Site"] for r in dc} == {"1", "2"}
    assert all(r["Site"] == "" for r in wan_rows)


def test_assign_underlay_sites_is_silent_for_a_single_fabric():
    from nornir_srl.connections.layer2 import assign_underlay_sites

    hosts = "192.0.2.11 192.0.2.12"
    rows = [
        {"Node": "leaf1", "System IPv4": "192.0.2.11", "Underlay Hosts": hosts, "Gateway": ""},
        {"Node": "leaf2", "System IPv4": "192.0.2.12", "Underlay Hosts": hosts, "Gateway": ""},
    ]
    assert assign_underlay_sites(rows) == {}


def test_vpn_tile_groups_reads_bare_string_route_targets():
    """SR Linux 26.x JSON_IETF reports import/export-rt as strings, not lists of dicts.

    That is what dci-srl DCGWs return for ``bgp-vpn`` instance 1 (DC) and 2 (WAN).
    """
    from nornir_srl.connections.layer2 import _vpn_tile_groups

    ni = {
        "protocols": {
            "bgp-evpn": {"bgp-instance": [{"id": 1, "admin-state": "enable"}]},
            "bgp-ipvpn": {"bgp-instance": [{"id": 2, "admin-state": "enable"}]},
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "route-target": {
                            "export-rt": "target:3000:3000",
                            "import-rt": "target:3000:3000",
                        },
                    },
                    {
                        "id": 2,
                        "route-target": {
                            "export-rt": "target:65000:3000",
                            "import-rt": "target:65000:3000",
                        },
                    },
                ]
            },
        }
    }
    groups = _vpn_tile_groups(ni, "none (isolated)")
    assert [(g["primary"], g["id"], g["gateway"]) for g in groups] == [
        ("target:3000:3000", "1", True),
        ("target:65000:3000", "2", True),
    ]


def test_vpn_tile_groups_splits_two_enabled_instances():
    from nornir_srl.connections.layer2 import _vpn_tile_groups

    ni = {
        "protocols": {
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "admin-state": "enable",
                        "route-target": {
                            "import-rt": [{"target": "target:65000:1"}],
                            "export-rt": [{"target": "target:65000:1"}],
                        },
                    },
                    {
                        "id": 2,
                        "admin-state": "enable",
                        "route-target": {
                            "import-rt": [{"target": "target:65500:1"}],
                            "export-rt": [{"target": "target:65500:1"}],
                        },
                    },
                ]
            }
        }
    }
    groups = _vpn_tile_groups(ni, "none (isolated)")
    assert len(groups) == 2
    assert groups[0] == {
        "primary": "target:65000:1",
        "rts": ["target:65000:1"],
        "id": "1",
        "gateway": True,
    }
    assert groups[1] == {
        "primary": "target:65500:1",
        "rts": ["target:65500:1"],
        "id": "2",
        "gateway": True,
    }


def test_vpn_tile_groups_disambiguates_shared_route_targets():
    from nornir_srl.connections.layer2 import _vpn_tile_groups

    ni = {
        "protocols": {
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "route-target": {
                            "export-rt": [{"target": "target:64500:1"}],
                            "import-rt": [{"target": "target:64500:1"}],
                        },
                    },
                    {
                        "id": 2,
                        "route-target": {
                            "export-rt": [{"target": "target:64500:1"}],
                            "import-rt": [{"target": "target:64500:1"}],
                        },
                    },
                ]
            }
        }
    }
    groups = _vpn_tile_groups(ni, "mac-vrf:BD1")
    assert [g["primary"] for g in groups] == [
        "target:64500:1 (bgp-instance 1)",
        "target:64500:1 (bgp-instance 2)",
    ]
    assert all(g["gateway"] for g in groups)


def test_vpn_tile_groups_ignores_a_disabled_second_instance():
    from nornir_srl.connections.layer2 import _vpn_tile_groups

    ni = {
        "protocols": {
            "bgp-vpn": {
                "bgp-instance": [
                    {
                        "id": 1,
                        "admin-state": "enable",
                        "route-target": {
                            "import-rt": [{"target": "target:65000:1"}],
                        },
                    },
                    {
                        "id": 2,
                        "admin-state": "disable",
                        "route-target": {
                            "import-rt": [{"target": "target:65500:1"}],
                        },
                    },
                ]
            }
        }
    }
    groups = _vpn_tile_groups(ni, "none (isolated)")
    assert len(groups) == 1
    assert groups[0]["gateway"] is False
    assert groups[0]["primary"] == "target:65000:1"
    assert groups[0]["rts"] == ["target:65000:1"]


def test_get_routers_emits_one_tile_per_gateway_bgp_instance():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    data = [
        {
            "network-instance": [
                {
                    "name": "ipvrf-3000",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "id": 1,
                                    "admin-state": "enable",
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:3000"}],
                                        "export-rt": [{"target": "target:65000:3000"}],
                                    },
                                },
                                {
                                    "id": 2,
                                    "admin-state": "enable",
                                    "route-target": {
                                        "import-rt": [{"target": "target:65500:3000"}],
                                        "export-rt": [{"target": "target:65500:3000"}],
                                    },
                                },
                            ]
                        }
                    },
                }
            ]
        }
    ]
    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    rows = dev.get_routers()["routers"]
    assert len(rows) == 2
    by_rt = {r["Router"]: r for r in rows}
    assert set(by_rt) == {"target:65000:3000", "target:65500:3000"}
    for rt, inst in (("target:65000:3000", "1"), ("target:65500:3000", "2")):
        assert by_rt[rt]["IP-VRF"] == "ipvrf-3000"
        assert by_rt[rt]["Route Targets"] == rt
        assert by_rt[rt]["Gateway"] == "Y"
        assert by_rt[rt]["BGP Instance"] == inst


def test_get_bridge_domains_emits_one_tile_per_gateway_bgp_instance():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    data = [
        {
            "network-instance": [
                {
                    "name": "BD1",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "id": 1,
                                    "admin-state": "enable",
                                    "route-target": {
                                        "import-rt": [{"target": "target:64500:1"}],
                                        "export-rt": [{"target": "target:64500:1"}],
                                    },
                                },
                                {
                                    "id": 2,
                                    "admin-state": "enable",
                                    "route-target": {
                                        "import-rt": [{"target": "target:64500:2"}],
                                        "export-rt": [{"target": "target:64500:2"}],
                                    },
                                },
                            ]
                        }
                    },
                }
            ]
        }
    ]
    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    rows = dev.get_bridge_domains()["bridge_domains"]
    assert len(rows) == 2
    by_rt = {r["Bridge Domain"]: r for r in rows}
    assert set(by_rt) == {"target:64500:1", "target:64500:2"}
    for rt, inst in (("target:64500:1", "1"), ("target:64500:2", "2")):
        assert by_rt[rt]["MAC-VRF"] == "BD1"
        assert by_rt[rt]["Route Targets"] == rt
        assert by_rt[rt]["Gateway"] == "Y"
        assert by_rt[rt]["BGP Instance"] == inst


def test_get_services():
    from nornir_srl.connections.layer2 import Layer2Mixin

    class _FakeLayer2(Layer2Mixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    data = [
        {
            "network-instance": [
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                },
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                },
            ]
        }
    ]

    dev = _FakeLayer2({"network-instance": data, "subinterface": [{}]})
    out = dev.get_services()
    services = out["services"]
    assert len(services) == 2
    types = [s["Service Type"] for s in services]
    assert "Bridge Domain" in types
    assert "Router" in types

# --------------------------------------------------------------------------- #
# Network-instance and interface getters
# --------------------------------------------------------------------------- #


_SUBITF_RESPONSE = [
    {
        "interface": [
            {
                "name": "ethernet-1/10",
                "subinterface": [
                    {
                        "index": 0,
                        "oper-state": "up",
                        "ip-mtu": 1500,
                        "ipv4": {"address": [{"ip-prefix": "192.168.1.1/30"}]},
                    }
                ],
            },
            {
                "name": "irb1",
                "subinterface": [
                    {
                        "index": 100,
                        "oper-state": "up",
                        "l2-mtu": 9000,
                        "ipv4": {"address": [{"ip-prefix": "10.1.100.1/24"}]},
                    }
                ],
            },
        ]
    }
]


def test_get_nwi_itf_joins_subinterface_details_onto_network_instances():
    ni_response = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "ethernet-1/10.0"}, {"name": "irb1.100"}],
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": [
                                {
                                    "route-target": {
                                        "import-rt": [{"target": "target:65000:1"}],
                                        "export-rt": [{"target": "target:65000:1"}],
                                    }
                                }
                            ]
                        }
                    },
                }
            ]
        }
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    rows = device.get_nwi_itf()["nwi_itfs"]

    assert len(rows) == 1
    row = rows[0]
    assert row["NI"] == "ip-vrf-1"
    assert row["oper"] == "up"
    assert row["In-RT"] == "65000:1"
    assert row["Out-RT"] == "65000:1"
    by_name = {i["Subitf"]: i for i in row["itfs"]}
    assert by_name["ethernet-1/10.0"]["ip-prefix"] == ["192.168.1.1/30"]
    assert by_name["ethernet-1/10.0"]["mtu"] == 1500
    # An IRB carries an l2-mtu rather than an ip-mtu.
    assert by_name["irb1.100"]["mtu"] == 9000
    assert by_name["irb1.100"]["if-oper"] == "up"


def test_get_nwi_itf_records_the_other_network_instance_an_irb_is_in():
    """An IRB sits in a mac-vrf and an ip-vrf; each row names the other one."""
    ni_response = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb1.100"}],
                },
                {
                    "name": "mac-vrf-100",
                    "type": "mac-vrf",
                    "oper-state": "up",
                    "interface": [{"name": "irb1.100"}],
                },
            ]
        }
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    rows = {r["NI"]: r for r in device.get_nwi_itf()["nwi_itfs"]}

    assert rows["ip-vrf-1"]["itfs"][0]["assoc-ni"] == "mac-vrf-100"
    assert rows["mac-vrf-100"]["itfs"][0]["assoc-ni"] == "ip-vrf-1"


def test_get_nwi_itf_accepts_a_single_network_instance_as_a_bare_dict():
    """gNMI returns a one-entry YANG list unwrapped, which must not crash."""
    ni_response = [
        {"network-instance": {"name": "default", "type": "default", "oper-state": "up"}}
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    assert device.get_nwi_itf()["nwi_itfs"] is not None


def test_get_nwi_itf_survives_an_empty_response():
    device = _FakeInterfaces({"subinterface": [], "network-instance": []})
    assert device.get_nwi_itf() == {"nwi_itfs": []}


def test_get_nwi_itf_reads_route_targets_from_import_and_export_policies():
    ni_response = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "type": "ip-vrf",
                    "oper-state": "up",
                    "protocols": {
                        "bgp-vpn": {
                            "bgp-instance": {
                                "import-policy": "import-all",
                                "export-policy": ["export-a", "export-b"],
                            }
                        }
                    },
                }
            ]
        }
    ]
    device = _FakeInterfaces(
        {"subinterface": _SUBITF_RESPONSE, "network-instance": ni_response}
    )

    row = device.get_nwi_itf()["nwi_itfs"][0]

    assert row["In-RT"] == "import-all"
    assert row["Out-RT"] == "export-a, export-b"


def test_get_lag_shortens_member_interface_names():
    lag_response = [
        {
            "interface": [
                {
                    "name": "lag1",
                    "oper-state": "up",
                    "mtu": 9000,
                    "lag": {
                        "lag-type": "lacp",
                        "min-links": 1,
                        "member": [
                            {
                                "name": "ethernet-1/1",
                                "oper-state": "up",
                                "lacp": {"activity": "ACTIVE"},
                            }
                        ],
                        "lacp": {"lacp-mode": "ACTIVE", "interval": "SLOW"},
                    },
                }
            ]
        }
    ]
    device = _FakeInterfaces({"interface": lag_response})

    rows = device.get_lag()["lag"]

    assert len(rows) == 1
    assert rows[0]["lag"] == "lag1"
    assert rows[0]["oper"] == "up"
    # Members are abbreviated so the column stays narrow enough to read.
    assert rows[0]["members"][0]["member-itf"] == "et-1/1"


def test_get_lag_survives_an_empty_response():
    device = _FakeInterfaces({"interface": []})
    assert device.get_lag() == {"lag": []}


def test_get_sum_subitf_names_subinterfaces_and_lists_addresses():
    device = _FakeInterfaces({"subinterface": _SUBITF_RESPONSE})

    rows = {r["Itf"]: r for r in device.get_sum_subitf()["subinterface"]}

    assert set(rows) == {"ethernet-1/10", "irb1"}
    subitf = rows["ethernet-1/10"]["subitfs"][0]
    assert subitf["Subitf"] == "ethernet-1/10.0"
    assert subitf["oper"] == "up"
    assert subitf["ipv4"] == ["192.168.1.1/30"]


def test_get_sum_subitf_survives_an_empty_response():
    device = _FakeInterfaces({"subinterface": []})
    assert device.get_sum_subitf() == {"subinterface": []}


def test_get_arp_and_nd_label_entries_with_the_network_instance():
    from nornir_srl.connections.neighbor_discovery import NeighborDiscoveryMixin

    class _FakeNeighbors(NeighborDiscoveryMixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    nis = [
        {
            "network-instance": [
                {
                    "name": "ip-vrf-1",
                    "interface": [{"name": "irb1.100"}],
                },
                {
                    "name": "mac-vrf-100",
                    "interface": [{"name": "irb1.100"}],
                },
            ]
        }
    ]
    arp = [
        {
            "interface": [
                {
                    "name": "irb1",
                    "subinterface": [
                        {
                            "index": 100,
                            "ipv4": {
                                "arp": {
                                    "neighbor": [
                                        {
                                            "ipv4-address": "10.1.100.10",
                                            "link-layer-address": "00:11:22:33:44:55",
                                            "origin": "dynamic",
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                }
            ]
        }
    ]
    nd = [
        {
            "interface": [
                {
                    "name": "irb1",
                    "subinterface": [
                        {
                            "index": 100,
                            "ipv6": {
                                "neighbor-discovery": {
                                    "neighbor": [
                                        {
                                            "ipv6-address": "2001:db8::10",
                                            "link-layer-address": "00:11:22:33:44:55",
                                            "origin": "dynamic",
                                            "current-state": "reachable",
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                }
            ]
        }
    ]
    device = _FakeNeighbors(
        {"network-instance": nis, "arp/neighbor": arp, "neighbor-discovery": nd}
    )

    arp_rows = device.get_arp()["arp"]
    assert len(arp_rows) == 1
    assert arp_rows[0]["interface"] == "irb1.100"
    assert arp_rows[0]["NI"] == "ip-vrf-1, mac-vrf-100"
    assert arp_rows[0]["entries"][0]["IPv4"] == "10.1.100.10"

    nd_rows = device.get_nd()["nd"]
    assert len(nd_rows) == 1
    assert nd_rows[0]["interface"] == "irb1.100"
    assert nd_rows[0]["NI"] == "ip-vrf-1, mac-vrf-100"
    assert nd_rows[0]["entries"][0]["IPv6"] == "2001:db8::10"


def test_get_arp_reads_a_single_interface_dict_on_the_network_instance():
    """gNMI often unwraps a one-entry YANG list to a dict; ARP must still bind NI."""
    from nornir_srl.connections.neighbor_discovery import NeighborDiscoveryMixin

    class _FakeNeighbors(NeighborDiscoveryMixin):
        def __init__(self, responses: Dict[str, List[Dict[str, Any]]]):
            self._responses = responses

        def get(
            self,
            paths: List[str],
            datatype: Optional[str] = "config",
            strip_mod: Optional[bool] = True,
        ) -> List[Dict[str, Any]]:
            path = paths[0]
            for key, resp in self._responses.items():
                if key in path:
                    return resp
            raise KeyError(f"no scripted response for path {path}")

    nis = [
        {
            "network-instance": {
                "name": "vrf1",
                "interface": {"name": "irb1.100"},
            }
        }
    ]
    arp = [
        {
            "interface": {
                "name": "irb1",
                "subinterface": {
                    "index": 100,
                    "ipv4": {
                        "arp": {
                            "neighbor": [
                                {
                                    "ipv4-address": "10.1.100.10",
                                    "link-layer-address": "00:11:22:33:44:55",
                                    "origin": "dynamic",
                                }
                            ]
                        }
                    },
                },
            }
        }
    ]
    device = _FakeNeighbors({"network-instance": nis, "arp/neighbor": arp})
    row = device.get_arp()["arp"][0]
    assert row["NI"] == "vrf1"
    assert row["interface"] == "irb1.100"
