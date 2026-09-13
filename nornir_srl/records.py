"""What a report getter returns, as data.

A getter reads one device and returns what it found. For the reports converted
so far it returns the records defined here rather than the rows of a table:
a network-instance with its subinterfaces as a list, a BGP neighbour with its
address families as objects that carry counts, a bridge-table entry with the
destination it points at already read apart. That is the contract every
reader of a payload - a check, a lens, the live server - is written against,
and it is what ``-o json`` and the MCP tools emit.

The table a report renders as is declared separately, next to its
:class:`~nornir_srl.reports.ReportSpec`, as the columns that read a record. A
column name never appears here: a record does not know what it will be
called on a screen, and a check does not have to know either.

A report that has not been converted still returns the ``dict`` items
:mod:`nornir_srl.rows` flattens; the two coexist until every report is over.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

# --------------------------------------------------------------------------- #
# records as objects
# --------------------------------------------------------------------------- #


def _plain(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    """A record's fields with its tuples as lists, which is what YAML can write."""
    return {key: list(value) if isinstance(value, tuple) else value for key, value in pairs}


def as_dict(record: Any) -> Dict[str, Any]:
    """*record* as the plain object ``-o json`` and the MCP tools emit."""
    return asdict(record, dict_factory=_plain)


def as_int(value: Any) -> Optional[int]:
    """*value* as an integer, or ``None`` when it is not one."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# mac: the bridge table
# --------------------------------------------------------------------------- #

#: A bridge-table destination as SR Linux writes the leaf: a bare subinterface
#: or ``irb-interface`` for a local entry; for one learned over VXLAN the
#: overlay interface and then either the VTEP that owns it or the
#: ethernet-segment it sits behind, ``vxlan-interface:vxlan1.101
#: vtep:192.168.255.2 vni:101``.
_DESTINATION = re.compile(
    r"vxlan-interface:(?P<overlay>\S+)"
    r"(?:\s+vtep:(?P<vtep>\S+))?"
    r"(?:\s+vni:(?P<vni>\d+))?"
    r"(?:\s+esi:(?P<esi>\S+))?"
)

#: One learned over EVPN-MPLS, as a gateway's WAN side has them: the far-end
#: PE and the label it is sent with, ``far-end:192.0.2.7 nh-tag:1727 label:310001``.
_FAR_END = re.compile(r"far-end:(?P<far_end>\S+)(?:\s+nh-tag:\S+)?(?:\s+label:(?P<label>\d+))?")


@dataclass(frozen=True)
class MacEntry:
    """One entry of a bridge table."""

    address: str
    #: Where it points, as the device writes it.
    destination: str
    #: How it got there: ``learnt``, ``evpn``, ``evpn-static``, ``irb-interface``...
    type: str
    #: The destination read apart. A local entry names the subinterface it was
    #: learned on (``irb-interface`` for the node's own gateway MAC); one
    #: learned over VXLAN names the overlay interface and either the VTEP it
    #: came from or the segment it sits behind; one learned over EVPN-MPLS
    #: names the far-end PE and the label.
    interface: str = ""
    overlay: str = ""
    vtep: str = ""
    vni: Optional[int] = None
    esi: str = ""
    far_end: str = ""
    label: Optional[int] = None

    @property
    def local(self) -> bool:
        """Learned on this node's own port, rather than from another node."""
        return not (self.overlay or self.far_end)

    @classmethod
    def read(cls, address: Any, destination: Any, type: Any) -> "MacEntry":
        """An entry from the three leaves the bridge table has for it."""
        dest = str(destination or "").strip()
        match = _DESTINATION.search(dest)
        if match:
            return cls(
                str(address or ""),
                dest,
                str(type or ""),
                overlay=match.group("overlay") or "",
                vtep=match.group("vtep") or "",
                vni=as_int(match.group("vni")),
                esi=match.group("esi") or "",
            )
        match = _FAR_END.search(dest)
        if match:
            return cls(
                str(address or ""),
                dest,
                str(type or ""),
                far_end=match.group("far_end"),
                label=as_int(match.group("label")),
            )
        return cls(str(address or ""), dest, str(type or ""), interface=dest)


@dataclass(frozen=True)
class BridgeTable:
    """The bridge table of one network-instance."""

    ni: str
    entries: Tuple[MacEntry, ...]


# --------------------------------------------------------------------------- #
# ni: network-instances and what is bound to them
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Subinterface:
    """A subinterface as a network-instance has it."""

    name: str
    oper: str
    prefixes: Tuple[str, ...] = ()
    mtu: Optional[int] = None
    vlan: Optional[int] = None
    #: For an irb: the other network-instances it is also in, which is how a
    #: mac-vrf names the ip-vrf its irb routes into.
    associated: Tuple[str, ...] = ()


@dataclass(frozen=True)
class BgpVpnInstance:
    """One bgp-vpn instance of a network-instance: what it imports and exports with.

    A leaf has one. A gateway has two, one per side of it - the DC side that
    shares its route-target with the leaves, and the WAN side with a
    route-target and a distinguisher of its own - so the two are kept apart
    rather than merged into one set the leaves would never match.
    """

    id: int
    #: Route-targets, or where a policy sets them instead, the policy's name.
    import_rts: Tuple[str, ...] = ()
    export_rts: Tuple[str, ...] = ()
    rd: str = ""


@dataclass(frozen=True)
class NetworkInstance:
    """One network-instance on one node."""

    name: str
    type: str
    oper: str
    router_id: str = ""
    #: The vxlan-interfaces bound to it.
    overlays: Tuple[str, ...] = ()
    #: The EVI of each bgp-evpn instance it advertises with; a gateway has two.
    evis: Tuple[str, ...] = ()
    instances: Tuple[BgpVpnInstance, ...] = ()
    interfaces: Tuple[Subinterface, ...] = ()

    @property
    def import_rts(self) -> Tuple[str, ...]:
        """Every route-target it imports with, over all its instances."""
        return tuple(sorted({rt for inst in self.instances for rt in inst.import_rts}))

    @property
    def export_rts(self) -> Tuple[str, ...]:
        return tuple(sorted({rt for inst in self.instances for rt in inst.export_rts}))


# --------------------------------------------------------------------------- #
# ifstats: what an interface carried over the last sample
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InterfaceStats:
    """The traffic of one interface, read from two counter samples.

    The rates and the error and discard counts are for the interval between
    the two samples - the pair the CLI takes *interval* seconds apart, or the
    last two the server streamed - so an error here is one happening now
    rather than one that once did. The packet and octet totals are the
    counters as the device keeps them.
    """

    name: str
    in_kbps: float = 0.0
    out_kbps: float = 0.0
    in_pps: float = 0.0
    out_pps: float = 0.0
    in_errors: int = 0
    out_errors: int = 0
    in_discards: int = 0
    out_discards: int = 0
    in_packets: int = 0
    out_packets: int = 0
    in_octets: int = 0
    out_octets: int = 0
    #: ``up``, ``down`` or ``down/standby``, where the sample carried the port
    #: state alongside its counters - the server streams both; the CLI reads
    #: only the counters and leaves this empty.
    oper: str = ""
    #: Why it is down, resolved to something one can act on - what tells an
    #: idle port that is meant to be idle from one that is not.
    down_reason: str = ""


# --------------------------------------------------------------------------- #
# subif: interfaces and the subinterfaces on them
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SubinterfaceState:
    """A subinterface as its interface has it: what it is and whether it is up.

    :class:`Subinterface` is the same thing seen from the network-instance it
    is bound to; this is its own view, with the state that is not visible
    from there.
    """

    name: str
    #: ``routed``, ``bridged``, or empty where the type is implied - an irb, a
    #: loopback.
    type: str = ""
    #: ``enable`` or ``disable``, as the device spells it.
    admin: str = ""
    #: ``up``, ``down``, or ``down/standby`` for one held down on purpose by
    #: the ethernet-segment its port is in.
    oper: str = ""
    #: The root cause of a down subinterface, resolved past the ``port-down``
    #: it says about itself to what its parent port says.
    down_reason: str = ""
    ip_mtu: Optional[int] = None
    vlan: Optional[int] = None
    ipv4: Tuple[str, ...] = ()
    ipv6: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Interface:
    """One interface and the subinterfaces on it."""

    name: str
    subinterfaces: Tuple[SubinterfaceState, ...] = ()


# --------------------------------------------------------------------------- #
# sys_info: what the node is
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SystemInfo:
    """The chassis and the software it runs."""

    type: str = ""
    serial_number: str = ""
    part_number: str = ""
    hw_mac_address: str = ""
    last_booted: str = ""
    #: The release alone, ``26.7.1``: the build tag the device appends is
    #: not what a matrix of releases is keyed on.
    software_version: str = ""


# --------------------------------------------------------------------------- #
# lag: link aggregation groups
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LagMember:
    """One port of a LAG."""

    #: The port as the device names it, ``ethernet-1/20``.
    name: str
    oper: str = ""
    #: LACP: ``ACTIVE`` or ``PASSIVE``.
    activity: str = ""


@dataclass(frozen=True)
class Lag:
    """One link aggregation group and the ports in it."""

    name: str
    oper: str = ""
    mtu: Optional[int] = None
    min_links: Optional[int] = None
    description: str = ""
    #: ``lacp`` or ``static``.
    type: str = ""
    speed: Optional[int] = None
    #: How the standby side of a single-active segment is signalled:
    #: ``lacp`` or ``power-off``.
    standby_signaling: str = ""
    lacp_key: Optional[int] = None
    lacp_interval: str = ""
    lacp_mode: str = ""
    lacp_system_id: str = ""
    lacp_priority: Optional[int] = None
    members: Tuple[LagMember, ...] = ()


# --------------------------------------------------------------------------- #
# vxlan: tunnel interfaces
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VxlanDestination:
    """One VTEP a vxlan-interface sends to, and the VNI it sends with."""

    vtep: str
    vni: Optional[int] = None


@dataclass(frozen=True)
class VxlanInterface:
    """One vxlan-interface, and where it sends unicast."""

    name: str
    ni: str
    #: The VNI it accepts on ingress.
    vni: Optional[int] = None
    destinations: Tuple[VxlanDestination, ...] = ()


# --------------------------------------------------------------------------- #
# es: ethernet segments
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NextHop:
    """A next-hop a virtual ethernet-segment tracks, and the EVIs it serves."""

    address: str
    evis: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    """One designated-forwarder candidate of a segment in a network-instance."""

    address: str
    designated: bool = False


@dataclass(frozen=True)
class Association:
    """A network-instance a segment is associated with, and its DF election."""

    ni: str
    candidates: Tuple[Candidate, ...] = ()

    @property
    def designated(self) -> Optional[str]:
        """The elected forwarder, or ``None`` when no election has concluded."""
        return next((c.address for c in self.candidates if c.designated), None)


@dataclass(frozen=True)
class EthernetSegment:
    """One ethernet-segment as one node has it configured."""

    name: str
    esi: str
    type: str
    mh_mode: str
    oper: str
    #: The ports it hangs off, or for a virtual segment the next-hops it tracks.
    interfaces: Tuple[str, ...] = ()
    next_hops: Tuple[NextHop, ...] = ()
    associations: Tuple[Association, ...] = ()


# --------------------------------------------------------------------------- #
# irb: the routed side of a bridge domain
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IrbAddress:
    """One address of an irb, and what it is there as."""

    prefix: str
    primary: bool = False
    #: The address every leaf of the domain answers on, as the anycast gateway.
    anycast_gw: bool = False


@dataclass(frozen=True)
class HostRouteRule:
    """Which neighbour entries an irb turns into host routes.

    ``populate`` names the origin of the entries - ``dynamic``, ``static``,
    ``evpn`` - and whether the routes are programmed into the datapath or
    only advertised.
    """

    route_type: str
    datapath_programming: bool = False


@dataclass(frozen=True)
class IrbArp:
    """The ARP settings of an irb."""

    proxy: bool = False
    learn_unsolicited: bool = False
    host_routes: Tuple[HostRouteRule, ...] = ()
    #: The entry origins advertised into EVPN as MAC/IP routes.
    evpn_advertise: Tuple[str, ...] = ()
    #: Whether the advertisement carries an interface-less-routing setting.
    interface_less_routing: bool = False


@dataclass(frozen=True)
class IrbNd:
    """The neighbour-discovery settings of an irb."""

    proxy: bool = False
    #: ``none``, ``global``, ``link-local`` or ``both``: unsolicited
    #: advertisements are learned for these address scopes.
    learn_unsolicited: str = ""
    host_routes: Tuple[HostRouteRule, ...] = ()
    evpn_advertise: Tuple[str, ...] = ()
    interface_less_routing: bool = False


@dataclass(frozen=True)
class IrbInterface:
    """One irb subinterface: the gateway a bridge domain routes through."""

    name: str
    #: The network-instances it is in: the mac-vrf it bridges and the ip-vrf
    #: it routes into.
    nis: Tuple[str, ...] = ()
    ipv4: Tuple[IrbAddress, ...] = ()
    ipv6: Tuple[IrbAddress, ...] = ()
    #: Configured as an anycast gateway, shared with the other leaves of the
    #: domain; the MAC is what they all answer with.
    anycast_gw: bool = False
    anycast_gw_mac: str = ""
    virtual_router_id: Optional[int] = None
    arp: IrbArp = IrbArp()
    nd: IrbNd = IrbNd()


# --------------------------------------------------------------------------- #
# es_dest: where the bridge table sends a segment's traffic
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EsDestination:
    """One ethernet-segment the bridge table forwards to, and the VTEPs behind it."""

    esi: str
    #: The vxlan-interface it is reached through, ``vxlan1.101``.
    overlay: str = ""
    vteps: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EsDestinations:
    """The ethernet-segment destinations of one tunnel-interface."""

    tunnel: str
    destinations: Tuple[EsDestination, ...] = ()


# --------------------------------------------------------------------------- #
# lldp: what is on the other end of each cable
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LldpNeighbor:
    """One neighbour an interface hears LLDP from."""

    #: The system name it advertises, which is how it is matched back to a
    #: node of the inventory.
    system_name: str
    port_id: str
    port_description: str = ""


@dataclass(frozen=True)
class LldpInterface:
    """One interface and the neighbours it hears."""

    name: str
    neighbors: Tuple[LldpNeighbor, ...] = ()


# --------------------------------------------------------------------------- #
# arp / nd: the neighbour caches
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NeighborEntry:
    """One ARP or ND entry: an address and the MAC it resolves to."""

    address: str
    mac: str
    #: ``dynamic``, ``static``, ``evpn``...
    origin: str = ""
    #: ND only: the reachability state, ``reachable``, ``stale``, ``delay``...
    state: str = ""
    #: Seconds until the entry ages out - for an ND entry, until it leaves
    #: its current state - or ``None`` where the device gives no time, as it
    #: does not for a static entry.
    expires_in: Optional[int] = None


@dataclass(frozen=True)
class NeighborCache:
    """The ARP or ND entries of one subinterface."""

    interface: str
    #: The network-instances the subinterface is bound to; an irb is in two.
    nis: Tuple[str, ...] = ()
    entries: Tuple[NeighborEntry, ...] = ()


# --------------------------------------------------------------------------- #
# bgp_peers: sessions and their address families
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Family:
    """One address family of a BGP session."""

    #: ``ipv4-unicast``, ``ipv6-unicast``, ``evpn``, ``l3vpn-ipv4-unicast``,
    #: ``l3vpn-ipv6-unicast``.
    name: str
    enabled: bool = True
    #: ``up``, ``down``, or empty where the device does not say.
    oper: str = ""
    received: int = 0
    active: int = 0
    sent: int = 0


@dataclass(frozen=True)
class Neighbor:
    """One BGP session as the node that holds it sees it."""

    peer: str
    state: str
    peer_as: Optional[int] = None
    local_as: Optional[int] = None
    local_address: str = ""
    local_port: Optional[int] = None
    group: str = ""
    dynamic: bool = False
    bfd: bool = False
    fast_failover: bool = False
    import_policies: Tuple[str, ...] = ()
    export_policies: Tuple[str, ...] = ()
    #: Only the families the session is configured for.
    families: Tuple[Family, ...] = ()

    def family(self, name: str) -> Optional[Family]:
        return next((f for f in self.families if f.name == name), None)


@dataclass(frozen=True)
class BgpPeers:
    """The BGP sessions of one network-instance."""

    ni: str
    neighbors: Tuple[Neighbor, ...]


# --------------------------------------------------------------------------- #
# ipv4_rib / ipv6_rib: the route tables
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Egress:
    """Where a next-hop leaves the node, resolved as far as the tables allow.

    A next-hop resolved to a port or a tunnel names it. An indirect one only
    names the route it resolves through, and is followed into that route's
    next-hop-group to find the port; where the chain cannot be walked to one,
    the prefix it stopped at is what there is to show.
    """

    #: ``interface``, ``tunnel`` or ``route``.
    kind: str
    #: The subinterface, the tunnel's endpoint prefix, or the prefix.
    value: str
    #: ``tunnel``: its type - ``vxlan``, ``ldp``, ``sr-isis``...
    tunnel: str = ""
    #: ``interface``: the network-instance the port is in, when that is not
    #: the route's own - which is how a leaked route leaves the node.
    ni: str = ""

    @property
    def label(self) -> str:
        """The port, ``vxlan:192.168.255.3/32``, or the prefix."""
        return f"{self.tunnel}:{self.value}" if self.kind == "tunnel" else self.value


@dataclass(frozen=True)
class RouteNextHop:
    """One next-hop of a route."""

    address: str = ""
    #: ``direct``, ``indirect``, ``extract``, ``discard``...
    type: str = ""
    #: An indirect next-hop names the prefix it resolves through.
    resolving_route: str = ""
    egress: Tuple[Egress, ...] = ()


@dataclass(frozen=True)
class Route:
    """One route of a route table."""

    prefix: str
    #: ``local``, ``host``, ``bgp``, ``bgp-evpn``, ``static``, ``arp-nd``...
    type: str
    active: bool = True
    metric: Optional[int] = None
    preference: Optional[int] = None
    #: The network-instance it was leaked from, when it was.
    leaked_from: str = ""
    next_hops: Tuple[RouteNextHop, ...] = ()


@dataclass(frozen=True)
class RouteTable:
    """The route table of one network-instance, for one address family."""

    ni: str
    routes: Tuple[Route, ...] = ()


# --------------------------------------------------------------------------- #
# static_routes: the routes someone typed in
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StaticNextHop:
    """One next-hop of a static route's next-hop-group."""

    address: str
    #: Resolved through the route table, rather than on a connected subnet.
    resolve: bool = False


@dataclass(frozen=True)
class StaticRoute:
    """One static route, with the next-hops its group names."""

    prefix: str
    #: ``enable`` or ``disable``, as the device spells it.
    admin: str = ""
    #: Whether it made it into the route table; ``None`` where the device did
    #: not say.
    installed: Optional[bool] = None
    metric: Optional[int] = None
    preference: Optional[int] = None
    next_hop_group: str = ""
    next_hops: Tuple[StaticNextHop, ...] = ()


@dataclass(frozen=True)
class StaticRouteTable:
    """The static routes of one network-instance."""

    ni: str
    routes: Tuple[StaticRoute, ...] = ()


# --------------------------------------------------------------------------- #
# tunnel_table: the transport tunnels
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TunnelNextHop:
    """One next-hop a tunnel is resolved onto."""

    address: str = ""
    subinterface: str = ""
    #: ``mpls``, ``direct``...
    type: str = ""
    #: The label stack pushed, outermost first - as strings, because a label
    #: can be a reserved name rather than a number.
    labels: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Tunnel:
    """One entry of a tunnel table: the endpoint and how it is reached."""

    prefix: str
    #: ``vxlan``, ``ldp``, ``sr-isis``, ``rsvp``...
    type: str = ""
    #: The application that installed it, ``vxlan_mgr``, ``ldp_mgr``...
    owner: str = ""
    preference: Optional[int] = None
    metric: Optional[int] = None
    next_hops: Tuple[TunnelNextHop, ...] = ()


@dataclass(frozen=True)
class TunnelTable:
    """The tunnel table of one network-instance."""

    ni: str
    tunnels: Tuple[Tunnel, ...] = ()


# --------------------------------------------------------------------------- #
# bgp_rib: the BGP RIBs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BgpRoute:
    """One route in a BGP RIB, with the path attributes it was received with.

    Which of the NLRI fields are set depends on the family and, for EVPN, on
    the route type: a MAC/IP route has a MAC and an IP, an IP prefix route a
    prefix and a gateway, an auto-discovery route an ESI and a tag, an IPv4 or
    VPN-IPv4 route a prefix and, for the latter, a route-distinguisher.
    """

    #: The peer it was received from; a locally originated route names
    #: ``0.0.0.0``.
    neighbor: str
    used: bool = False
    valid: bool = False
    best: bool = False
    rd: str = ""
    prefix: str = ""
    esi: str = ""
    tag: Optional[int] = None
    mac: str = ""
    ip: str = ""
    gateway: str = ""
    #: The VNI it is advertised with; on a MAC/IP route the two labels the
    #: NLRI carries, of which the first is the VNI.
    vni: Optional[int] = None
    label1: Optional[int] = None
    label2: Optional[int] = None
    next_hop: str = ""
    origin: str = ""
    local_pref: Optional[int] = None
    med: Optional[int] = None
    as_path: Tuple[int, ...] = ()
    #: Read out of the extended communities: the route-targets, the ESI
    #: labels (``Single-Active/0``), the sites of origin and the tunnel
    #: encapsulations.
    route_targets: Tuple[str, ...] = ()
    esi_labels: Tuple[str, ...] = ()
    soo: Tuple[str, ...] = ()
    tunnel_encap: Tuple[str, ...] = ()
    #: The communities as carried: standard, large and extended.
    communities: Tuple[str, ...] = ()
    large_communities: Tuple[str, ...] = ()
    ext_communities: Tuple[str, ...] = ()
    #: The D-PATH domain ids, in order.
    domain_path: Tuple[str, ...] = ()
    tie_break: str = ""
    internal_tags: Tuple[str, ...] = ()
    neighbor_as: Optional[int] = None


@dataclass(frozen=True)
class BgpRib:
    """The BGP RIB of one network-instance, for one family and route type."""

    ni: str
    #: ``evpn``, ``ipv4-unicast``, ``ipv6-unicast``, ``l3vpn-ipv4-unicast``,
    #: ``l3vpn-ipv6-unicast``.
    family: str
    #: For EVPN, the route type ``1`` to ``5``.
    route_type: str = ""
    routes: Tuple[BgpRoute, ...] = ()


__all__ = [
    "Association",
    "BgpPeers",
    "BgpRib",
    "BgpRoute",
    "BgpVpnInstance",
    "BridgeTable",
    "Candidate",
    "Egress",
    "EsDestination",
    "EsDestinations",
    "EthernetSegment",
    "Family",
    "HostRouteRule",
    "Interface",
    "InterfaceStats",
    "IrbAddress",
    "IrbArp",
    "IrbInterface",
    "IrbNd",
    "Lag",
    "LagMember",
    "LldpInterface",
    "LldpNeighbor",
    "MacEntry",
    "Neighbor",
    "NeighborCache",
    "NeighborEntry",
    "NetworkInstance",
    "NextHop",
    "Route",
    "RouteNextHop",
    "RouteTable",
    "StaticNextHop",
    "StaticRoute",
    "StaticRouteTable",
    "Subinterface",
    "SubinterfaceState",
    "SystemInfo",
    "Tunnel",
    "TunnelNextHop",
    "TunnelTable",
    "VxlanDestination",
    "VxlanInterface",
    "as_dict",
    "as_int",
]
