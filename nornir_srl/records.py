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
#: or ``irb-interface`` for a local entry; for a remote one the overlay
#: interface and then either the VTEP that owns it or the ethernet-segment it
#: sits behind, ``vxlan-interface:vxlan1.101 vtep:192.168.255.2 vni:101``.
_DESTINATION = re.compile(
    r"vxlan-interface:(?P<overlay>\S+)"
    r"(?:\s+vtep:(?P<vtep>\S+))?"
    r"(?:\s+vni:(?P<vni>\d+))?"
    r"(?:\s+esi:(?P<esi>\S+))?"
)


@dataclass(frozen=True)
class MacEntry:
    """One entry of a bridge table."""

    address: str
    #: Where it points, as the device writes it.
    destination: str
    #: How it got there: ``learnt``, ``evpn``, ``evpn-static``, ``irb-interface``...
    type: str
    #: The destination read apart. A local entry names the subinterface it was
    #: learned on (``irb-interface`` for the node's own gateway MAC); a remote
    #: one names the overlay interface and either the VTEP it came from or the
    #: segment it sits behind.
    interface: str = ""
    overlay: str = ""
    vtep: str = ""
    vni: Optional[int] = None
    esi: str = ""

    @property
    def local(self) -> bool:
        """Learned on this node's own port, rather than over the overlay."""
        return not self.overlay

    @classmethod
    def read(cls, address: Any, destination: Any, type: Any) -> "MacEntry":
        """An entry from the three leaves the bridge table has for it."""
        dest = str(destination or "").strip()
        match = _DESTINATION.search(dest)
        if not match:
            return cls(str(address or ""), dest, str(type or ""), interface=dest)
        return cls(
            str(address or ""),
            dest,
            str(type or ""),
            overlay=match.group("overlay") or "",
            vtep=match.group("vtep") or "",
            vni=as_int(match.group("vni")),
            esi=match.group("esi") or "",
        )


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
    #: Route-targets, or where a policy sets them instead, the policy's name.
    import_rts: Tuple[str, ...] = ()
    export_rts: Tuple[str, ...] = ()
    interfaces: Tuple[Subinterface, ...] = ()


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
    "BridgeTable",
    "Candidate",
    "Egress",
    "EthernetSegment",
    "Family",
    "MacEntry",
    "Neighbor",
    "NetworkInstance",
    "NextHop",
    "Route",
    "RouteNextHop",
    "RouteTable",
    "Subinterface",
    "VxlanDestination",
    "VxlanInterface",
    "as_dict",
    "as_int",
]
