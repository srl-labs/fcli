"""The single registry of the reports fcli can produce.

A report is one table: a getter from :mod:`nornir_srl.connections` plus the
metadata needed to present it. All three surfaces are driven from this list -
the ``fcli`` commands, the ``fcli-mcp`` tools and the live tables of
``fcli server`` - so a report is defined once and cannot drift between them.

Which surfaces a report appears on is part of its spec (:attr:`ReportSpec.surfaces`),
because not every report suits every surface. The executive ``overview`` only
means something in the browser; ``bgp_rib`` takes an address family the streaming
server has no way to supply, so the server gets one pre-bound report per family
instead; ``routing_pol`` returns nested JSON that no table can represent.

Getters are called as ``spec.getter(device, **params)``, where *params* are the
arguments the surface collected (a CLI option, an MCP tool argument). Every
parameter has a default, so a surface that has nothing to pass - the server -
can always call ``spec.getter(device)``.

An argument a *user* supplies, rather than one the surface chooses, is declared
in :attr:`ReportSpec.params` as well: the CLI and MCP surfaces name their own
options and tool arguments, but the browser has nothing to go on but the report
registry, so a parameter it is meant to collect has to describe and validate
itself.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

CLI = "cli"
MCP = "mcp"
SERVER = "server"
ALL_SURFACES: FrozenSet[str] = frozenset({CLI, MCP, SERVER})
INTERACTIVE: FrozenSet[str] = frozenset({CLI, MCP})
STREAMING: FrozenSet[str] = frozenset({SERVER})


@dataclass(frozen=True)
class SubscriptionSpec:
    """One gNMI subscription entry."""

    path: str
    #: ``state``, ``config`` or ``all``.
    #:
    #: A subscription on a single leaf that SR Linux defines as *config* -
    #: ``network-instance/type``, ``interface/admin-state``,
    #: ``system/name/host-name`` - has to ask for ``all``. A path is
    #: bootstrapped with a gNMI ``Get`` before it can be streamed, and a ``Get``
    #: with datatype ``state`` answers nothing at all for a config leaf: not an
    #: error, an empty response. The path then stays pending forever and
    #: whatever reads it renders as if the node had no such data. Subtrees are
    #: not affected - ``/network-instance[name=*]`` returns the config leaves
    #: inside it either way.
    datatype: str = "state"
    mode: str = "sample"  # sample | on_change | target_defined
    sample_interval: int = 10  # seconds

    def as_gnmi(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"path": self.path, "mode": self.mode}
        if self.mode == "sample":
            entry["sample_interval"] = int(self.sample_interval * 1_000_000_000)
        return entry


@dataclass(frozen=True)
class ParamSpec:
    """One argument a report takes from whoever is looking at it.

    Enough for a surface to ask for it without knowing which report it belongs
    to: what to call it, what a plausible value looks like, and what counts as
    one.
    """

    #: The keyword the getter takes, and the query argument it arrives in.
    name: str
    label: str
    placeholder: str = ""
    help: str = ""
    #: ``text``, or ``address`` for one that has to parse as an IP address.
    kind: str = "text"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "placeholder": self.placeholder,
            "help": self.help,
            "kind": self.kind,
        }

    def coerce(self, value: Any) -> Optional[str]:
        """*value* as the getter wants it, or ``None`` when it is not set.

        Raises :class:`ValueError` with something worth showing to whoever
        typed it.
        """
        text = str(value or "").strip()
        if not text:
            return None
        if self.kind == "address":
            try:
                return str(ipaddress.ip_address(text))
            except ValueError:
                raise ValueError(
                    f"{self.label}: '{text}' is not an IP address"
                ) from None
        return text


@dataclass(frozen=True)
class ReportSpec:
    """One report, on every surface that offers it."""

    #: Canonical id, and the CLI command name (Typer renders ``_`` as ``-``).
    name: str
    #: Key under which the getter returns its rows.
    resource: str
    title: str
    description: str
    #: Called as ``getter(device, **params)``.
    getter: Callable[..., Dict[str, Any]]
    category: str = "General"
    #: MCP tool name, where a more explicit one reads better for an agent.
    mcp_name: Optional[str] = None
    surfaces: FrozenSet[str] = ALL_SURFACES
    #: gNMI SAMPLE interval (seconds) for the paths this report subscribes to.
    sample_interval: int = 15
    #: Explicit subscriptions; when empty the server discovers the paths by
    #: running the getter once against a RecordingDevice.
    subscribe: Tuple[SubscriptionSpec, ...] = ()
    #: False when the payload nests too deeply for a table to represent.
    tabular: bool = True
    #: Arguments a user supplies, for the surfaces that can collect them.
    #: Every one is optional, and a report renders in full without them.
    params: Tuple[ParamSpec, ...] = ()
    #: The columns that identify a row rather than describe it, which is what
    #: comparing two renderings of this report keys on. Spelled as the rendered
    #: table spells them: the ``<n>_`` that only orders a column is gone by
    #: then, and so are the newlines that wrap a header.
    #:
    #: A report that declares none can still be compared - every difference
    #: then reads as one row gone and another arrived, rather than as a row
    #: that changed.
    key_columns: Tuple[str, ...] = ()

    @property
    def tool_name(self) -> str:
        """The name this report is exposed under on the MCP surface."""
        return self.mcp_name or self.name

    def on(self, surface: str) -> bool:
        return surface in self.surfaces

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "sample_interval": self.sample_interval,
            "params": [p.as_dict() for p in self.params],
            # The browser offers a comparison either way, and says which kind
            # it can be: without keys, a change reads as an add and a remove.
            "key_columns": list(self.key_columns),
        }


def coerce_params(report: ReportSpec, raw: Mapping[str, Any]) -> Dict[str, Any]:
    """The parameters *report* declares, out of a surface's raw input.

    Anything it does not declare is ignored rather than handed on: the query
    string of a live table also carries the refresh interval and the inventory
    filter, which are the server's business and not the getter's.
    """
    params: Dict[str, Any] = {}
    for spec in report.params:
        value = spec.coerce(raw.get(spec.name, ""))
        if value is not None:
            params[spec.name] = value
    return params


def _bound_bgp_rib(
    route_fam: str, route_type: Optional[str] = None
) -> Callable[..., Dict[str, Any]]:
    """A ``bgp_rib`` getter with its address family already chosen."""

    def getter(device: Any) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"route_fam": route_fam, "detail": False}
        if route_type is not None:
            kwargs["route_type"] = route_type
        return device.get_bgp_rib(**kwargs)

    return getter


def _bgp_rib(
    device: Any,
    route_fam: str = "evpn",
    route_type: Optional[str] = None,
    detail: bool = False,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"route_fam": route_fam, "detail": detail}
    if route_type is not None:
        kwargs["route_type"] = route_type
    return device.get_bgp_rib(**kwargs)


def _lpm_param(example: str) -> ParamSpec:
    """The address the RIB reports look up, as the CLI's ``-a`` does.

    Left empty the report is the whole route table, which is what it is for.
    Filled in, each route table keeps only the one prefix of it that the
    address falls into - the route the node would actually forward on.
    """
    return ParamSpec(
        name="address",
        label="LPM",
        placeholder=f"LPM lookup, e.g. {example}",
        help="Longest prefix matching this address, per node and route table",
        kind="address",
    )


#: Every service report reads the same two trees.
_SERVICE_SUBSCRIPTIONS: Tuple[SubscriptionSpec, ...] = (
    SubscriptionSpec("/network-instance[name=*]", datatype="all", sample_interval=20),
    SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=20),
    # A member reported 'port-down' is explained by its parent port, and that
    # is what says whether a standby ethernet-segment or a fault put it there.
    SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=20),
    # The segment a multi-homed service's members hang off, whose mode and DF
    # say which leaf forwards for it.
    SubscriptionSpec(
        "/system/network-instance/protocols/evpn/ethernet-segments",
        datatype="all",
        sample_interval=20,
    ),
    SubscriptionSpec(
        "/network-instance[name=default]/route-table/ipv4-unicast/route/ipv4-prefix",
        datatype="state",
        sample_interval=20,
    ),
    SubscriptionSpec(
        "/network-instance[name=default]/route-table/ipv6-unicast/route/ipv6-prefix",
        datatype="state",
        sample_interval=20,
    ),
)

#: Resolving a route's next-hop needs the next-hop(-group) tables alongside it.
_NEXT_HOP_SUBSCRIPTIONS: Tuple[SubscriptionSpec, ...] = (
    SubscriptionSpec("/network-instance[name=*]/route-table/next-hop-group[index=*]", datatype="state"),
    SubscriptionSpec("/network-instance[name=*]/route-table/next-hop[index=*]", datatype="state"),
)


def _bgp_rib_variants() -> List[ReportSpec]:
    """One streaming report per BGP address family.

    A streamed report cannot be given arguments, so the server needs the address
    family baked in. EVPN is split further, by route type, because a fabric's
    EVPN RIB is far too large to read as one table.
    """
    evpn = [
        ("1", "type 1 (A-D)", "EVPN auto-discovery routes received from BGP peers."),
        ("2", "type 2 (MAC/IP)", "EVPN MAC/IP advertisement routes received from BGP peers."),
        ("3", "type 3 (IMET)", "EVPN inclusive multicast ethernet tag routes."),
        ("4", "type 4 (ES)", "EVPN ethernet segment routes."),
        ("5", "type 5 (IP prefix)", "EVPN IP prefix routes."),
    ]
    families = [
        ("ipv4", "ipv4", "IPv4 unicast", "IPv4 unicast"),
        ("ipv6", "ipv6", "IPv6 unicast", "IPv6 unicast"),
        ("l3vpn_v4", "l3vpn-ipv4-unicast", "L3VPN IPv4", "VPN-IPv4 unicast"),
        ("l3vpn_v6", "l3vpn-ipv6-unicast", "L3VPN IPv6", "VPN-IPv6 unicast"),
    ]
    variants = [
        ReportSpec(
            name=f"bgp_rib_evpn_{route_type}",
            resource="bgp_rib",
            title=f"BGP RIB - EVPN {label}",
            description=description,
            getter=_bound_bgp_rib("evpn", route_type),
            category="BGP RIB",
            surfaces=STREAMING,
        )
        for route_type, label, description in evpn
    ]
    variants.extend(
        ReportSpec(
            name=f"bgp_rib_{suffix}",
            resource="bgp_rib",
            title=f"BGP RIB - {label}",
            description=f"{noun} routes in the BGP RIB-in-post.",
            getter=_bound_bgp_rib(route_fam),
            category="BGP RIB",
            surfaces=STREAMING,
        )
        for suffix, route_fam, label, noun in families
    )
    return variants


REPORTS: List[ReportSpec] = [
    ReportSpec(
        name="overview",
        resource="overview",
        title="Overview",
        description="Executive Fabric KPI dashboard and health metrics.",
        # Computed by the store from the streamed trees below, not by a getter.
        getter=lambda d: {},
        category="Dashboard",
        surfaces=STREAMING,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/statistics", sample_interval=10),
            # 'admin-state', 'description' and 'type' are config leaves: see
            # SubscriptionSpec.
            SubscriptionSpec("/interface[name=*]/admin-state", datatype="all", sample_interval=10),
            SubscriptionSpec("/interface[name=*]/oper-state", sample_interval=10),
            # A port an ethernet-segment holds in standby is down by design, and
            # this is what keeps it out of the 'oper down' count.
            SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=10),
            SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=10),
            SubscriptionSpec("/interface[name=*]/description", datatype="all", sample_interval=10),
            SubscriptionSpec("/interface[name=*]/ethernet", datatype="all", sample_interval=10),
            SubscriptionSpec("/network-instance[name=*]/type", datatype="all", sample_interval=10),
            SubscriptionSpec("/network-instance[name=*]/oper-state", sample_interval=10),
            SubscriptionSpec("/network-instance[name=*]/protocols/bgp/neighbor", datatype="all", sample_interval=10),
            SubscriptionSpec("/network-instance[name=*]/protocols/bgp-vpn", datatype="all", sample_interval=10),
        ),
    ),
    ReportSpec(
        name="topology",
        resource="topology",
        title="Topology",
        description="Fabric graph from LLDP, with the tier of each node inferred from its services.",
        # Computed by the store from the streamed trees below, not by a getter.
        getter=lambda d: {},
        category="Dashboard",
        surfaces=STREAMING,
        # Paths that mix config leaves ('host-name', 'type') are read as 'all':
        # a 'state' Get answers nothing for those (see SubscriptionSpec). The
        # chassis type is state-only, the same path the sys-info report uses.
        subscribe=(
            # LLDP gives the cables; the host-name is what a neighbour is
            # advertised under, and the only reliable way back to the inventory.
            SubscriptionSpec(
                "/system/lldp/interface[name=*]/neighbor",
                datatype="all",
                sample_interval=30,
            ),
            SubscriptionSpec("/system/name/host-name", datatype="all", sample_interval=30),
            # Chassis type, drawn on each node. Sampled rarely: it does not
            # change without a hardware swap.
            SubscriptionSpec("/platform/chassis", datatype="state", sample_interval=60),
            SubscriptionSpec("/interface[name=*]/oper-state", datatype="all", sample_interval=30),
            # Which of the down ports are only standing by, so the cable to a
            # multi-homed client is not drawn from the leaf that is not
            # forwarding.
            SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=30),
            # Egress of each interface, so each end of a cable can be coloured
            # from the rate leaving that port. Sampled often enough that a lab
            # generating traffic will move the graph with it.
            SubscriptionSpec("/interface[name=*]/statistics", sample_interval=5),
            # A node's tier follows from the services on it: mac-vrfs and
            # ip-vrfs make it a leaf, two bgp-vpn instances make it a DCGW.
            SubscriptionSpec("/network-instance[name=*]/type", datatype="all", sample_interval=30),
            SubscriptionSpec(
                "/network-instance[name=*]/protocols/bgp-vpn",
                datatype="all",
                sample_interval=30,
            ),
            # The client tier: which subinterfaces a service is configured
            # towards, and the vlan and address each of them attaches on.
            SubscriptionSpec(
                "/network-instance[name=*]/interface",
                datatype="all",
                sample_interval=30,
            ),
            SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=30),
            # The ESI a port is in, which is what says the lags of a multi-homed
            # client on two leaves are one client rather than two.
            SubscriptionSpec(
                "/system/network-instance/protocols/evpn/ethernet-segments",
                datatype="all",
                sample_interval=30,
            ),
        ),
    ),
    ReportSpec(
        name="sys_info",
        resource="sys_info",
        key_columns=("Node",),
        title="System Info",
        description="Chassis type, serial, software version and last boot time.",
        getter=lambda d: d.get_info(),
        category="System",
        sample_interval=60,
        subscribe=(
            SubscriptionSpec("/platform/chassis", datatype="state"),
            SubscriptionSpec("/platform/control[slot=A]", datatype="state"),
        ),
    ),
    ReportSpec(
        name="ifstats",
        resource="ifstats",
        key_columns=("Node", "interface"),
        title="Interface Stats",
        description="Per-interface rates and error/discard counters, derived from "
        "streamed gNMI counter samples.",
        getter=lambda d, interval=5: d.get_ifstats(interval=interval),
        category="Interfaces",
        sample_interval=5,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/statistics", sample_interval=5),
            SubscriptionSpec("/interface[name=*]/oper-state", sample_interval=30),
            SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=30),
        ),
    ),
    ReportSpec(
        name="subif",
        resource="subinterface",
        key_columns=("Node", "Subitf"),
        title="Sub-Interfaces",
        description="Sub-interfaces with their type, addresses and operational state.",
        getter=lambda d: d.get_sum_subitf(),
        category="Interfaces",
        mcp_name="subinterfaces",
        sample_interval=20,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=20),
            SubscriptionSpec("/interface[name=*]/oper-down-reason", sample_interval=20),
        ),
    ),
    ReportSpec(
        name="lag",
        resource="lag",
        key_columns=("Node", "lag", "member-itf"),
        title="LAGs",
        description="Link aggregation groups and their members.",
        getter=lambda d: d.get_lag(),
        category="Interfaces",
        sample_interval=20,
        subscribe=(
            SubscriptionSpec("/interface[name=lag*]", datatype="all", sample_interval=20),
        ),
    ),
    ReportSpec(
        name="ni",
        resource="nwi_itfs",
        key_columns=("Node", "NI", "Subitf"),
        title="Network Instances",
        description="Network instances and the interfaces bound to them.",
        getter=lambda d: d.get_nwi_itf(),
        category="Interfaces",
        mcp_name="network_instances",
        sample_interval=30,
        subscribe=(
            SubscriptionSpec("/interface[name=*]/subinterface", datatype="all", sample_interval=30),
            SubscriptionSpec("/network-instance[name=*]", datatype="all", sample_interval=30),
        ),
    ),
    ReportSpec(
        name="bgp_peers",
        resource="bgp_peers",
        key_columns=("Node", "NI", "peer"),
        title="BGP Peers",
        description="BGP neighbors, their session state and per-AF route counters.",
        getter=lambda d: d.get_sum_bgp(),
        category="BGP",
        sample_interval=10,
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/protocols/bgp/neighbor", datatype="all", sample_interval=10),
        ),
    ),
    ReportSpec(
        name="bgp_rib",
        resource="bgp_rib",
        title="BGP RIB",
        description="Routes in the BGP RIB-in-post with their path attributes.",
        getter=_bgp_rib,
        category="BGP RIB",
        surfaces=INTERACTIVE,
    ),
    *_bgp_rib_variants(),
    ReportSpec(
        name="ipv4_rib",
        resource="ip_rib",
        # A prefix can be offered by more than one protocol at once, so the
        # route type is part of what names a route rather than of what it says.
        key_columns=("Node", "NI", "Prefix", "type"),
        title="IPv4 RIB",
        description="IPv4 route table with resolved next-hops.",
        getter=lambda d, address=None: d.get_rib(
            afi="ipv4-unicast", lpm_address=address
        ),
        category="Routing",
        params=(_lpm_param("10.0.0.1"),),
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/route-table/ipv4-unicast", datatype="state"),
        )
        + _NEXT_HOP_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="ipv6_rib",
        resource="ip_rib",
        key_columns=("Node", "NI", "Prefix", "type"),
        title="IPv6 RIB",
        description="IPv6 route table with resolved next-hops.",
        getter=lambda d, address=None: d.get_rib(
            afi="ipv6-unicast", lpm_address=address
        ),
        category="Routing",
        params=(_lpm_param("2001:db8::1"),),
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/route-table/ipv6-unicast", datatype="state"),
        )
        + _NEXT_HOP_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="static_routes",
        resource="static_routes",
        key_columns=("Node", "NI", "route"),
        title="Static Routes",
        description="Configured static routes and their operational state.",
        getter=lambda d: d.get_static_routes(),
        category="Routing",
        sample_interval=30,
        subscribe=(
            SubscriptionSpec("/network-instance[name=*]/static-routes", datatype="all", sample_interval=30),
        ),
    ),
    ReportSpec(
        name="tunnel_table",
        resource="tunnel_table",
        key_columns=("Node", "NI", "Prefix", "type"),
        title="Tunnel Table",
        description="IP tunnel table (VXLAN, LDP, SR-ISIS, RSVP, ...).",
        getter=lambda d: d.get_tunnel_table(),
        category="Routing",
    ),
    ReportSpec(
        name="routing_pol",
        resource="routing_pol",
        title="Routing Policies",
        description="Routing policies with their statements and actions.",
        getter=lambda d: d.get_routing_policies(),
        category="Routing",
        mcp_name="routing_policies",
        # Policies nest arbitrarily deep, so there are no columns to derive.
        surfaces=INTERACTIVE,
        tabular=False,
    ),
    ReportSpec(
        name="services",
        resource="services",
        key_columns=("Node", "Service Type", "MAC-VRF", "IP-VRF"),
        title="Services",
        description="EVPN Bridge Domains (MAC-VRF) and Routers (IP-VRF) grouped by "
        "Route-Target.",
        getter=lambda d: d.get_services(),
        category="Services",
        surfaces=STREAMING,
        sample_interval=20,
        subscribe=_SERVICE_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="bridge_domains",
        resource="bridge_domains",
        key_columns=("Node", "MAC-VRF"),
        title="Bridge Domains",
        description="EVPN Bridge Domains (MAC-VRF) grouped by Route-Target with bound "
        "access sub-interfaces, their ethernet-segments and VXLAN overlays.",
        getter=lambda d: d.get_bridge_domains(),
        category="Services",
        surfaces=STREAMING,
        sample_interval=20,
        subscribe=_SERVICE_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="routers",
        resource="routers",
        key_columns=("Node", "IP-VRF"),
        title="Routers",
        description="EVPN Routers (IP-VRF) grouped by Route-Target with bound MAC-VRFs, "
        "routed sub-interfaces, virtual ethernet-segments and VXLAN overlays.",
        getter=lambda d: d.get_routers(),
        category="Services",
        surfaces=STREAMING,
        sample_interval=20,
        subscribe=_SERVICE_SUBSCRIPTIONS,
    ),
    ReportSpec(
        name="mac",
        resource="mac_table",
        key_columns=("Node", "NI", "Address"),
        title="MAC Table",
        description="Bridge table MAC entries per network instance.",
        getter=lambda d: d.get_mac_table(),
        category="EVPN / L2",
        mcp_name="mac_table",
        sample_interval=10,
    ),
    ReportSpec(
        name="irb",
        resource="irb",
        key_columns=("Node", "name"),
        title="IRB Interfaces",
        description="IRB sub-interfaces and their anycast gateway configuration.",
        getter=lambda d: d.get_irb(),
        category="EVPN / L2",
        mcp_name="irb_interfaces",
        sample_interval=30,
        subscribe=(
            SubscriptionSpec("/interface[name=irb*]/subinterface", datatype="all", sample_interval=30),
            SubscriptionSpec("/network-instance[name=*]", datatype="config", sample_interval=30),
        ),
    ),
    ReportSpec(
        name="es",
        resource="es",
        key_columns=("Node", "name"),
        title="Ethernet Segments",
        description="EVPN ethernet segments, multi-homing mode and DF state.",
        getter=lambda d: d.get_es(),
        category="EVPN / L2",
        mcp_name="ethernet_segments",
        sample_interval=20,
    ),
    ReportSpec(
        name="es_dest",
        resource="es_dest",
        key_columns=("Node", "tunnel", "esi"),
        title="L2-ES Destinations",
        description="Ethernet segment destinations in the bridge table.",
        getter=lambda d: d.get_es_dest(),
        category="EVPN / L2",
        mcp_name="es_destinations",
        sample_interval=20,
    ),
    ReportSpec(
        name="vxlan",
        resource="vxlan",
        key_columns=("Node", "vxlan-itf"),
        title="VXLAN Tunnels",
        description="VXLAN tunnel interfaces and their unicast destinations.",
        getter=lambda d: d.get_vxlan(),
        category="EVPN / L2",
        mcp_name="vxlan_tunnels",
        sample_interval=20,
    ),
    ReportSpec(
        name="lldp",
        resource="lldp_nbrs",
        key_columns=("Node", "interface", "Nbr-System", "Nbr-port"),
        title="LLDP Neighbors",
        description="LLDP neighbors seen on each interface.",
        getter=lambda d: d.get_lldp_sum(),
        category="Neighbors",
        mcp_name="lldp_neighbors",
        sample_interval=20,
        subscribe=(
            SubscriptionSpec("/system/lldp/interface[name=*]/neighbor", datatype="state", sample_interval=20),
        ),
    ),
    ReportSpec(
        name="arp",
        resource="arp",
        key_columns=("Node", "interface", "IPv4"),
        title="ARP Table",
        description="IPv4 ARP / neighbor entries per sub-interface.",
        getter=lambda d: d.get_arp(),
        category="Neighbors",
        mcp_name="arp_table",
        subscribe=(
            SubscriptionSpec("/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor", datatype="all"),
            SubscriptionSpec("/network-instance[name=*]", datatype="config"),
        ),
    ),
    ReportSpec(
        name="nd",
        resource="nd",
        key_columns=("Node", "interface", "IPv6"),
        title="IPv6 Neighbors",
        description="IPv6 neighbor discovery entries per sub-interface.",
        getter=lambda d: d.get_nd(),
        category="Neighbors",
        mcp_name="ipv6_neighbors",
        subscribe=(
            SubscriptionSpec("/interface[name=*]/subinterface[index=*]/ipv6/neighbor-discovery/neighbor", datatype="all"),
            SubscriptionSpec("/network-instance[name=*]", datatype="config"),
        ),
    ),
    ReportSpec(
        name="checks",
        resource="checks",
        key_columns=("Node", "Check", "Subject"),
        title="Checks",
        description="Fabric sanity checks: BGP sessions, interfaces, LLDP adjacencies, "
        "MTU, EVPN service consistency and ethernet-segment DF election.",
        # A finding is about the fabric rather than about one node, so this is
        # not collected per device. Each surface runs nornir_srl.checks over the
        # reports the checks declare, which is where the gNMI work happens.
        getter=lambda d: {},
        category="Dashboard",
        mcp_name="fabric_checks",
        sample_interval=20,
    ),
]

REPORTS_BY_NAME: Dict[str, ReportSpec] = {r.name: r for r in REPORTS}


def get_report(name: str) -> ReportSpec:
    """Look a report up by its canonical name."""
    try:
        return REPORTS_BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown report '{name}'") from None


def reports_for(surface: str) -> List[ReportSpec]:
    """Every report offered on *surface*, in registry order."""
    return [r for r in REPORTS if r.on(surface)]
