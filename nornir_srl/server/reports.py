"""Registry of the reports the fcli server can stream.

Every entry reuses a getter from :mod:`nornir_srl.connections`, so a report
shown in the browser has exactly the same content as the corresponding ``fcli``
command. The gNMI paths each report needs are discovered at runtime by running
the getter once against a :class:`~nornir_srl.server.devices.RecordingDevice`,
which keeps the path definitions in a single place (the getters themselves).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .stream import SubscriptionSpec


@dataclass(frozen=True)
class Report:
    """A table the server can render and keep up to date."""

    name: str
    title: str
    description: str
    resource: str
    getter: Callable[[Any], Dict[str, Any]]
    category: str = "General"
    #: gNMI SAMPLE interval (seconds) for the paths this report subscribes to.
    sample_interval: int = 15
    #: Explicit subscriptions; when empty the paths are auto-discovered.
    subscribe: List[SubscriptionSpec] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "sample_interval": self.sample_interval,
        }


def _bgp_rib(
    route_fam: str, route_type: Optional[str] = None
) -> Callable[[Any], Dict[str, Any]]:
    def getter(device: Any) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"route_fam": route_fam, "detail": False}
        if route_type is not None:
            kwargs["route_type"] = route_type
        return device.get_bgp_rib(**kwargs)

    return getter


REPORTS: List[Report] = [
    Report(
        name="sys_info",
        title="System Info",
        description="Chassis type, serial, software version and last boot time.",
        resource="sys_info",
        getter=lambda d: d.get_info(),
        category="System",
        sample_interval=60,
    ),
    Report(
        name="ifstats",
        title="Interface Stats",
        description="Per-interface rates and error/discard counters, derived from "
        "streamed gNMI counter samples.",
        resource="ifstats",
        getter=lambda d: d.get_ifstats(),
        category="Interfaces",
        sample_interval=5,
        subscribe=[
            SubscriptionSpec("/interface[name=*]/statistics", sample_interval=5),
            SubscriptionSpec("/interface[name=*]/oper-state", sample_interval=30),
        ],
    ),
    Report(
        name="subif",
        title="Sub-Interfaces",
        description="Sub-interfaces with their type, addresses and operational state.",
        resource="subinterface",
        getter=lambda d: d.get_sum_subitf(),
        category="Interfaces",
        sample_interval=20,
    ),
    Report(
        name="lag",
        title="LAGs",
        description="Link aggregation groups and their members.",
        resource="lag",
        getter=lambda d: d.get_lag(),
        category="Interfaces",
        sample_interval=20,
    ),
    Report(
        name="ni",
        title="Network Instances",
        description="Network instances and the interfaces bound to them.",
        resource="nwi_itfs",
        getter=lambda d: d.get_nwi_itf(),
        category="Interfaces",
        sample_interval=30,
    ),
    Report(
        name="bgp_peers",
        title="BGP Peers",
        description="BGP neighbors, their session state and per-AF route counters.",
        resource="bgp_peers",
        getter=lambda d: d.get_sum_bgp(),
        category="BGP",
        sample_interval=10,
    ),
    Report(
        name="bgp_rib_evpn_1",
        title="BGP RIB - EVPN type 1 (A-D)",
        description="EVPN auto-discovery routes received from BGP peers.",
        resource="bgp_rib",
        getter=_bgp_rib("evpn", "1"),
        category="BGP RIB",
    ),
    Report(
        name="bgp_rib_evpn_2",
        title="BGP RIB - EVPN type 2 (MAC/IP)",
        description="EVPN MAC/IP advertisement routes received from BGP peers.",
        resource="bgp_rib",
        getter=_bgp_rib("evpn", "2"),
        category="BGP RIB",
    ),
    Report(
        name="bgp_rib_evpn_3",
        title="BGP RIB - EVPN type 3 (IMET)",
        description="EVPN inclusive multicast ethernet tag routes.",
        resource="bgp_rib",
        getter=_bgp_rib("evpn", "3"),
        category="BGP RIB",
    ),
    Report(
        name="bgp_rib_evpn_4",
        title="BGP RIB - EVPN type 4 (ES)",
        description="EVPN ethernet segment routes.",
        resource="bgp_rib",
        getter=_bgp_rib("evpn", "4"),
        category="BGP RIB",
    ),
    Report(
        name="bgp_rib_evpn_5",
        title="BGP RIB - EVPN type 5 (IP prefix)",
        description="EVPN IP prefix routes.",
        resource="bgp_rib",
        getter=_bgp_rib("evpn", "5"),
        category="BGP RIB",
    ),
    Report(
        name="bgp_rib_ipv4",
        title="BGP RIB - IPv4 unicast",
        description="IPv4 unicast routes in the BGP RIB-in-post.",
        resource="bgp_rib",
        getter=_bgp_rib("ipv4"),
        category="BGP RIB",
    ),
    Report(
        name="bgp_rib_ipv6",
        title="BGP RIB - IPv6 unicast",
        description="IPv6 unicast routes in the BGP RIB-in-post.",
        resource="bgp_rib",
        getter=_bgp_rib("ipv6"),
        category="BGP RIB",
    ),
    Report(
        name="bgp_rib_l3vpn_v4",
        title="BGP RIB - L3VPN IPv4",
        description="VPN-IPv4 unicast routes in the BGP RIB-in-post.",
        resource="bgp_rib",
        getter=_bgp_rib("l3vpn-ipv4-unicast"),
        category="BGP RIB",
    ),
    Report(
        name="bgp_rib_l3vpn_v6",
        title="BGP RIB - L3VPN IPv6",
        description="VPN-IPv6 unicast routes in the BGP RIB-in-post.",
        resource="bgp_rib",
        getter=_bgp_rib("l3vpn-ipv6-unicast"),
        category="BGP RIB",
    ),
    Report(
        name="ipv4_rib",
        title="IPv4 RIB",
        description="IPv4 route table with resolved next-hops.",
        resource="ip_rib",
        getter=lambda d: d.get_rib(afi="ipv4-unicast"),
        category="Routing",
    ),
    Report(
        name="ipv6_rib",
        title="IPv6 RIB",
        description="IPv6 route table with resolved next-hops.",
        resource="ip_rib",
        getter=lambda d: d.get_rib(afi="ipv6-unicast"),
        category="Routing",
    ),
    Report(
        name="static_routes",
        title="Static Routes",
        description="Configured static routes and their operational state.",
        resource="static_routes",
        getter=lambda d: d.get_static_routes(),
        category="Routing",
        sample_interval=30,
    ),
    Report(
        name="tunnel_table",
        title="Tunnel Table",
        description="IP tunnel table (VXLAN, LDP, SR-ISIS, RSVP, ...).",
        resource="tunnel_table",
        getter=lambda d: d.get_tunnel_table(),
        category="Routing",
    ),
    Report(
        name="mac",
        title="MAC Table",
        description="Bridge table MAC entries per network instance.",
        resource="mac_table",
        getter=lambda d: d.get_mac_table(),
        category="EVPN / L2",
        sample_interval=10,
    ),
    Report(
        name="irb",
        title="IRB Interfaces",
        description="IRB sub-interfaces and their anycast gateway configuration.",
        resource="irb",
        getter=lambda d: d.get_irb(),
        category="EVPN / L2",
        sample_interval=30,
    ),
    Report(
        name="es",
        title="Ethernet Segments",
        description="EVPN ethernet segments, multi-homing mode and DF state.",
        resource="es",
        getter=lambda d: d.get_es(),
        category="EVPN / L2",
        sample_interval=20,
    ),
    Report(
        name="es_dest",
        title="L2-ES Destinations",
        description="Ethernet segment destinations in the bridge table.",
        resource="es_dest",
        getter=lambda d: d.get_es_dest(),
        category="EVPN / L2",
        sample_interval=20,
    ),
    Report(
        name="vxlan",
        title="VXLAN Tunnels",
        description="VXLAN tunnel interfaces and their unicast destinations.",
        resource="vxlan",
        getter=lambda d: d.get_vxlan(),
        category="EVPN / L2",
        sample_interval=20,
    ),
    Report(
        name="lldp",
        title="LLDP Neighbors",
        description="LLDP neighbors seen on each interface.",
        resource="lldp_nbrs",
        getter=lambda d: d.get_lldp_sum(),
        category="Neighbors",
        sample_interval=20,
    ),
    Report(
        name="arp",
        title="ARP Table",
        description="IPv4 ARP / neighbor entries per sub-interface.",
        resource="arp",
        getter=lambda d: d.get_arp(),
        category="Neighbors",
    ),
    Report(
        name="nd",
        title="IPv6 Neighbors",
        description="IPv6 neighbor discovery entries per sub-interface.",
        resource="nd",
        getter=lambda d: d.get_nd(),
        category="Neighbors",
    ),
]

REPORTS_BY_NAME: Dict[str, Report] = {r.name: r for r in REPORTS}


def get_report(name: str) -> Report:
    try:
        return REPORTS_BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown report '{name}'") from None
