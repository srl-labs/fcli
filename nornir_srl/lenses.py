"""Lenses: a question asked of the fabric, answered as one table.

A report renders one node's state. A check asks the fabric a fixed question and
answers yes or no. Neither is what someone troubleshooting actually types, which
is closer to *where is this MAC*, *how would this node reach that address*, and
*what does this service look like everywhere it exists*. Each of those is one
answer joined out of several reports across several nodes, so none of them fits
:class:`~nornir_srl.reports.ReportSpec` - a report's getter is handed one device
and cannot see the fabric it sits in.

A lens is the missing shape: it reads a :class:`~nornir_srl.fabric.FabricState`
the same way a check does, takes arguments the way a report does, and returns
what it found the way both do. :data:`LENSES` is the single registry, so a lens
is defined once and the CLI command and the MCP tool are generated from it.

A lens answers in two layers. The function itself returns *records* - one
:class:`Sighting`, :class:`Hop` or :class:`Service` per thing found - holding
the answer as data: a list of VTEPs is a list, a count is a number, and what
kind of thing was found is a field rather than a word to match on. That is what
``-o json`` and the MCP tools emit, so whatever reads the answer by machine gets
fields rather than a sentence to parse. The table is built from those records by
the lens's :attr:`~LensSpec.columns`: each :class:`Column` names itself and says
how a record fills it, so a column name lives in exactly one place, and the
sentence in a ``Detail`` cell is composed for a reader rather than parsed by one.

Adding one means writing a record type, a function over a :class:`FabricState`
that returns a list of them, the columns that render one, and an entry in
:data:`LENSES`.

Some of what these functions do is string parsing that should not be necessary:
a bridge-table destination arrives as ``"vxlan-interface:vxlan1.101
vtep:192.168.255.2 vni:101"`` because the getter formatted it for display before
anything could read it. Where that happens it is parsed here rather than worked
around, and marked with a comment, so that the eventual split between payload
and presentation has a list of callers to fix.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, replace
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .aliases import resolve
from .fabric import FabricState, as_list, out_of_band, parent, text
from .reports import INTERACTIVE, ParamSpec

#: How far a path walk follows the fabric before deciding it is going in
#: circles. A datacenter fabric is three tiers; anything beyond this is a loop
#: the walk should report rather than keep following.
MAX_HOPS = 16


# --------------------------------------------------------------------------- #
# records: what a lens answers, as data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Sighting:
    """One place in the fabric that knows about an address.

    Which of the optional fields are filled depends on :attr:`kind`.
    """

    node: str
    ni: str
    #: ``arp`` or ``neighbor``: an address binding that named the MAC.
    #: ``local``: a bridge-table entry learned on this node's own port.
    #: ``remote``: one learned over the overlay, from a VTEP or behind a
    #: segment. ``duplicate``: learned locally here and on :attr:`also_on` as
    #: well. ``not-found``: no node has it.
    kind: str
    #: The IP a binding was found for, or the MAC.
    address: str
    #: What the entry is on: the subinterface a binding or a local entry was
    #: learned on, the VTEP a remote one came from, or the segment it is
    #: behind. Exactly one of the three is set for anything that was found.
    interface: str = ""
    vtep: str = ""
    esi: str = ""
    #: How the entry got there, as the table says it: ``learnt``, ``evpn``,
    #: ``static``, ``dynamic``.
    origin: str = ""
    #: ``arp``/``neighbor``: the MAC the binding resolved to, and when it goes.
    mac: str = ""
    expiry: str = ""
    #: ``remote``: the overlay interface it was learned over, and the VNI.
    overlay: str = ""
    vni: str = ""
    #: ``remote`` behind a segment: what the segment is called on the nodes
    #: that have it configured. Empty when none of the collected nodes do.
    segments: Tuple[str, ...] = ()
    #: ``duplicate``: the other nodes that learned it locally.
    also_on: Tuple[str, ...] = ()
    #: ``not-found``: how many nodes' bridge tables were searched.
    searched: int = 0


@dataclass(frozen=True)
class Hop:
    """One step of a walk through the route tables.

    A hop is one lookup on one node and what the walk did with the result.
    Every ECMP branch and every tunnel is a hop of its own, so several hops
    can share a number. Which of the optional fields are filled depends on
    :attr:`outcome`.
    """

    hop: int
    node: str
    ni: str
    #: The address looked up here: the destination, or the VTEP the walk is
    #: chasing through the underlay on behalf of a VRF.
    address: str
    #: What the walk did with the lookup.
    #: ``forwarded``: out of :attr:`egress` to :attr:`peer`, where it goes on.
    #: ``dead-end``: out of an interface with no LLDP neighbour, so it cannot.
    #: ``overlay``: resolved to a tunnel; it goes on in the underlay towards
    #: :attr:`vtep`. ``vtep-reached``: the underlay delivered the VTEP; it
    #: resumes in :attr:`resumes_in`. ``delivered``: the destination is
    #: attached here. ``local-ip``: it is this node's own address.
    #: ``neighbor``/``no-neighbor``: whether ARP or ND has the delivered
    #: address. ``no-route``, ``loop``, ``too-long``: where a walk gives up.
    outcome: str
    #: The route the lookup matched, as the route table has it.
    prefix: str = ""
    route_type: str = ""
    next_hops: Tuple[str, ...] = ()
    #: The subinterface the packet leaves on, or the tunnel it takes.
    egress: str = ""
    #: The node on the other end of that cable, and its port.
    peer: str = ""
    peer_port: str = ""
    #: ``overlay``: the VTEP the tunnel leads to.
    vtep: str = ""
    #: ``vtep-reached``: the network-instance the walk picks up again in.
    resumes_in: str = ""
    #: ``neighbor``: the MAC the binding resolved to, and how it was learned.
    mac: str = ""
    origin: str = ""
    #: ``loop``: the steps already taken, as ``node/network-instance``.
    visited: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Interface:
    """A subinterface of a network-instance, and whether it is up."""

    name: str
    oper: str


@dataclass(frozen=True)
class Service:
    """One network-instance as one node carries it."""

    node: str
    ni: str
    type: str
    oper: str
    #: The EVI of each BGP-EVPN instance it advertises with; a gateway has two.
    evis: Tuple[str, ...]
    #: The ingress VNI of each overlay interface bound to it.
    vnis: Tuple[str, ...]
    import_rts: Tuple[str, ...]
    export_rts: Tuple[str, ...]
    interfaces: Tuple[Interface, ...]
    #: The instances attached to it: the ip-vrf an irb of a mac-vrf routes
    #: into, or the mac-vrfs an ip-vrf routes for.
    bound: Tuple[str, ...]
    #: The VTEPs its overlay sends to.
    vteps: Tuple[str, ...]
    local_macs: int
    remote_macs: int
    #: The ethernet-segments associated with it.
    segments: Tuple[str, ...]


def _plain(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    """A record's fields with its tuples as lists, which is what YAML can write."""
    return {key: list(value) if isinstance(value, tuple) else value for key, value in pairs}


def as_dict(record: Any) -> Dict[str, Any]:
    """*record* as the plain object ``-o json`` and the MCP tools emit."""
    return asdict(record, dict_factory=_plain)


# --------------------------------------------------------------------------- #
# rows: how a record reads in a table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Column:
    """One table column: what it is called, and how a record fills it."""

    name: str
    #: The record field to show, or a function of the record for a cell that
    #: is composed rather than copied.
    cell: Union[str, Callable[[Any], Any]]

    def of(self, record: Any) -> Any:
        if isinstance(self.cell, str):
            return getattr(record, self.cell)
        return self.cell(record)


@dataclass(frozen=True)
class LensSpec:
    """One question, on every surface that offers it."""

    #: Canonical id, and the CLI command name (Typer renders ``_`` as ``-``).
    name: str
    title: str
    description: str
    #: What the reports it reads are called in the report registry. These are
    #: what a surface has to collect before it can call :attr:`run`.
    requires: Tuple[str, ...]
    #: How a record reads as a row, in the order the columns read. ``Node``
    #: is every record's own and the table renders it itself, so no lens
    #: declares it.
    columns: Tuple[Column, ...]
    #: Called as ``run(state, **params)``; returns the records.
    run: Callable[..., List[Any]]
    params: Tuple[ParamSpec, ...] = ()
    #: MCP tool name, where a more explicit one reads better for an agent.
    mcp_name: Optional[str] = None
    surfaces: FrozenSet[str] = INTERACTIVE
    #: False when consecutive rows of one node are one answer rather than a
    #: list of them, and the table should keep them in the order produced.
    group_by_node: bool = True

    @property
    def tool_name(self) -> str:
        return self.mcp_name or self.name

    @property
    def column_names(self) -> List[str]:
        return [column.name for column in self.columns]

    def on(self, surface: str) -> bool:
        return surface in self.surfaces

    def row(self, record: Any) -> Dict[str, Any]:
        """*record* as one table row, keyed by column name."""
        return {
            "Node": record.node or "-",
            **{column.name: column.of(record) for column in self.columns},
        }

    def rows(self, records: Iterable[Any]) -> List[Dict[str, Any]]:
        return [self.row(record) for record in records]


# --------------------------------------------------------------------------- #
# reading the parts of a payload that were formatted before anyone read them
# --------------------------------------------------------------------------- #

#: A bridge-table destination, as ``get_mac_table`` writes it. Local entries are
#: a bare subinterface or ``irb-interface``; a remote one names the overlay and
#: then either the VTEP that owns it or the ethernet-segment it sits behind.
_DEST = re.compile(
    r"vxlan-interface:(?P<overlay>\S+)"
    r"(?:\s+vtep:(?P<vtep>\S+))?"
    r"(?:\s+vni:(?P<vni>\d+))?"
    r"(?:\s+esi:(?P<esi>\S+))?"
)

#: A VXLAN destination list, as ``get_vxlan`` writes it: ``(vtep, vni), ...``.
_VTEP = re.compile(r"\((?P<vtep>[^,\s]+),\s*(?P<vni>\d+)\)")

#: An egress that is not an interface but a tunnel, which is how a route
#: resolved over the overlay reports itself: ``vxlan:192.168.255.3/32``.
_TUNNEL = re.compile(r"^vxlan:(?P<vtep>[0-9a-fA-F.:]+)(?:/\d+)?$")

#: Route types that terminate a walk because the destination is attached to the
#: node that holds them rather than reachable through it.
_ATTACHED = ("local", "host", "direct", "arp-nd", "static-local")


@dataclass(frozen=True)
class Destination:
    """Where a bridge-table entry points."""

    #: ``local``, ``irb``, ``vtep`` or ``esi``.
    kind: str
    #: The subinterface, VTEP address or ESI, whichever names it.
    via: str
    overlay: str = ""
    vni: str = ""

    @property
    def local(self) -> bool:
        return self.kind in ("local", "irb")


def parse_destination(dest: Any) -> Destination:
    """Read a bridge-table destination string back into its parts."""
    value = str(dest or "").strip()
    if not value:
        return Destination(kind="unknown", via="")
    match = _DEST.search(value)
    if not match:
        if value.startswith("irb-interface"):
            return Destination(kind="irb", via=value)
        return Destination(kind="local", via=value)
    esi, vtep = match.group("esi"), match.group("vtep")
    return Destination(
        kind="esi" if esi else "vtep",
        via=esi or vtep or "",
        overlay=match.group("overlay") or "",
        vni=match.group("vni") or "",
    )


def parse_vteps(destinations: Any) -> List[Tuple[str, str]]:
    """The ``(vtep, vni)`` pairs a VXLAN interface sends to."""
    return [(m.group("vtep"), m.group("vni")) for m in _VTEP.finditer(str(destinations or ""))]


def parse_listed(value: Any) -> Tuple[str, ...]:
    """A list a getter joined with ``", "`` - route-targets, EVIs, overlays."""
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


def _address(value: Any) -> Optional[Any]:
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None


def _network(value: Any) -> Optional[Any]:
    try:
        return ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        return None


def _mac(value: Any) -> str:
    """A MAC as the bridge table writes it, or ``""`` if it is not one."""
    raw = str(value or "").strip().upper().replace("-", ":")
    octets = raw.split(":")
    if len(octets) == 6 and all(re.fullmatch(r"[0-9A-F]{1,2}", o) for o in octets):
        return ":".join(o.zfill(2) for o in octets)
    return ""


def _joined(values: Iterable[Any], limit: int = 4) -> str:
    """A short list as one cell, saying how many it left out."""
    items = [str(v) for v in values if str(v)]
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", +{len(items) - limit} more"


# --------------------------------------------------------------------------- #
# where: locating an address in the fabric
# --------------------------------------------------------------------------- #


def _arp_bindings(state: FabricState) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """Every ARP and ND entry in the fabric, as (node, interface entry, entry)."""
    bindings = []
    for report, key in (("arp", "IPv4"), ("nd", "IPv6")):
        for node, itf, entry in state.sub_items(report, "entries"):
            bindings.append((node, itf, {**entry, "_addr": entry.get(key), "_report": report}))
    return bindings


def _es_by_esi(state: FabricState) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
    """Ethernet segments indexed by ESI, so a remote MAC can name its segment."""
    segments: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for node, entry in state.items("es"):
        esi = str(entry.get("esi") or "")
        if esi:
            segments.setdefault(esi, []).append((node, entry))
    return segments


def lens_where(state: FabricState, target: str = "") -> List[Sighting]:
    """Every place in the fabric that knows about one MAC or IP address.

    Answers the question a bridge-table dump does not: not *what is in this
    node's table*, but *which node owns this address, which node learned it
    over the overlay, and do two of them disagree about that*.

    An IP is resolved through ARP or ND to a MAC first, so ``where 10.0.1.51``
    and ``where 00:C1:AB:00:01:21`` converge on the same answer.
    """
    wanted = str(target or "").strip()
    if not wanted:
        raise ValueError("where needs a MAC or IP address to look for")

    sightings: List[Sighting] = []
    mac = _mac(wanted)
    address = _address(wanted)
    if not mac and address is None:
        raise ValueError(f"'{wanted}' is neither a MAC nor an IP address")

    # An IP is only ever a way of naming a MAC here: resolve it, report the
    # bindings that did so, and carry on with what they resolved to.
    if address is not None:
        for node, itf, entry in _arp_bindings(state):
            if _address(entry.get("_addr")) != address:
                continue
            bound = _mac(entry.get("MAC"))
            mac = mac or bound
            sightings.append(
                Sighting(
                    node=node,
                    ni=str(itf.get("NI", "")),
                    kind="arp" if entry["_report"] == "arp" else "neighbor",
                    address=str(address),
                    interface=str(itf.get("interface", "")),
                    origin=text(entry.get("Type")),
                    mac=bound or str(entry.get("MAC") or ""),
                    expiry=str(entry.get("expiry") or ""),
                )
            )
        if not mac:
            return sightings

    segments = _es_by_esi(state)
    # Network-instance -> the sightings learned locally in it, by position.
    local: Dict[str, List[int]] = {}
    for node, ni_entry, entry in state.sub_items("mac", "Fib"):
        if _mac(entry.get("Address")) != mac:
            continue
        ni = str(ni_entry.get("NI", ""))
        # The destination arrives pre-formatted, so it is read back apart here.
        dest = parse_destination(entry.get("Dest"))
        origin = text(entry.get("Type"))
        if dest.local:
            local.setdefault(ni, []).append(len(sightings))
            sightings.append(
                Sighting(node, ni, "local", mac, interface=dest.via, origin=origin)
            )
        elif dest.kind == "esi":
            sightings.append(
                Sighting(
                    node,
                    ni,
                    "remote",
                    mac,
                    esi=dest.via,
                    origin=origin,
                    overlay=dest.overlay,
                    vni=dest.vni,
                    segments=tuple(
                        sorted({str(es.get("name")) for _n, es in segments.get(dest.via, [])})
                    ),
                )
            )
        else:
            sightings.append(
                Sighting(
                    node,
                    ni,
                    "remote",
                    mac,
                    vtep=dest.via,
                    origin=origin,
                    overlay=dest.overlay,
                    vni=dest.vni,
                )
            )

    # Two nodes both owning one MAC locally is legitimate when they are the two
    # sides of an all-active segment, and is a duplicate or a silent move
    # otherwise. The bridge table cannot say which, so say that it cannot: each
    # local sighting becomes a duplicate naming the others.
    for indexes in local.values():
        owners = sorted({sightings[i].node for i in indexes})
        if len(owners) < 2:
            continue
        for i in indexes:
            sighting = sightings[i]
            sightings[i] = replace(
                sighting,
                kind="duplicate",
                also_on=tuple(node for node in owners if node != sighting.node),
            )

    if not sightings:
        sightings.append(
            Sighting(
                node="",
                ni="",
                kind="not-found",
                address=mac or wanted,
                searched=len(state.nodes("mac")),
            )
        )
    return sightings


def _sighting_detail(sighting: Sighting) -> str:
    """What a reader wants to know about a sighting beyond where it is."""
    if sighting.kind in ("arp", "neighbor"):
        detail = f"{sighting.mac or '?'}, {sighting.origin}"
        return detail + (f", expires {sighting.expiry}" if sighting.expiry else "")
    if sighting.kind == "not-found":
        return f"no node reports it in any bridge table ({sighting.searched} searched)"
    detail = sighting.origin
    if sighting.vtep:
        detail += f", overlay {sighting.overlay}"
        detail += f", vni {sighting.vni}" if sighting.vni else ""
    elif sighting.esi:
        detail += f", segment {', '.join(sighting.segments) or 'not local'}"
    if sighting.also_on:
        detail += (
            f"; also learned locally on {', '.join(sighting.also_on)}: expected on "
            "an all-active segment, a move or a duplicate otherwise"
        )
    return detail


WHERE_COLUMNS: Tuple[Column, ...] = (
    Column("NI", "ni"),
    Column("Found", "kind"),
    Column("Address", "address"),
    Column("Via", lambda s: s.interface or s.vtep or s.esi),
    Column("Detail", _sighting_detail),
)


# --------------------------------------------------------------------------- #
# path: how the fabric would forward towards an address
# --------------------------------------------------------------------------- #


def _rib_report(address: Any) -> str:
    return "ipv6_rib" if getattr(address, "version", 4) == 6 else "ipv4_rib"


def _routes(state: FabricState, report: str, node: str, ni: str) -> List[Dict[str, Any]]:
    """The active routes of one network-instance on one node."""
    routes = []
    for entry_node, entry in state.items(report):
        if entry_node != node or str(entry.get("NI", "")) != ni:
            continue
        for route in as_list(entry.get("Rib")):
            if isinstance(route, dict) and text(route.get("Act")) in ("yes", "true", ""):
                routes.append(route)
    return routes


def _lpm(routes: Sequence[Dict[str, Any]], address: Any) -> Optional[Dict[str, Any]]:
    """The longest prefix among *routes* that contains *address*."""
    best, best_len = None, -1
    for route in routes:
        network = _network(route.get("Prefix"))
        if network is None or network.version != address.version:
            continue
        if address in network and network.prefixlen > best_len:
            best, best_len = route, network.prefixlen
    return best


def _lldp_peers(state: FabricState) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """(node, interface) -> the (node, port) on the other end of that cable."""
    index = state.alias_index()
    peers: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for node, entry in state.items("lldp"):
        port = str(entry.get("interface", ""))
        if out_of_band(port):
            continue
        for neighbor in as_list(entry.get("Neighbors")):
            if not isinstance(neighbor, dict):
                continue
            advertised = str(neighbor.get("Nbr-System") or "")
            resolved = resolve(advertised, index) if advertised else None
            if resolved:
                peers[(node, port)] = (resolved, str(neighbor.get("Nbr-port") or ""))
    return peers


def _neighbor_index(state: FabricState) -> Dict[Tuple[str, str], List[Tuple[str, str, str]]]:
    """(node, address) -> every (interface, MAC, origin) ARP or ND binds it to."""
    index: Dict[Tuple[str, str], List[Tuple[str, str, str]]] = {}
    for node, itf, entry in _arp_bindings(state):
        address = _address(entry.get("_addr"))
        if address is not None:
            index.setdefault((node, str(address)), []).append(
                (
                    str(itf.get("interface", "")),
                    str(entry.get("MAC") or ""),
                    text(entry.get("Type")),
                )
            )
    return index


def _starting_nodes(state: FabricState, source: str, ni: str) -> List[str]:
    """Where a walk begins: a named node, or whoever knows the source address."""
    known = state.nodes("ipv4_rib") or state.nodes("lldp")
    index = state.alias_index()
    resolved = resolve(source, index)
    if resolved:
        return [resolved]

    address = _address(source)
    if address is None:
        raise ValueError(f"'{source}' is neither a node in the inventory nor an address")

    # An address starts the walk wherever it is directly attached: an ARP or ND
    # binding for it, or a connected route covering it.
    starts = []
    for node, itf, entry in _arp_bindings(state):
        if _address(entry.get("_addr")) == address and node not in starts:
            starts.append(node)
    if starts:
        return starts
    for node in known:
        route = _lpm(_routes(state, _rib_report(address), node, ni), address)
        if route and text(route.get("type")) in ("local", "host", "direct"):
            starts.append(node)
    if not starts:
        raise ValueError(
            f"no node has {address} directly attached in network-instance '{ni}'; "
            "name the node to start from instead"
        )
    return starts


#: One lookup still to do: the node and network-instance to do it in, the
#: address to look up, the hop it is on, the steps taken to get there, and -
#: when it is an underlay leg chasing a VTEP on behalf of a VRF - the
#: network-instance and address to resume with once the VTEP is reached.
_Pending = Tuple[str, str, Any, int, Tuple[str, ...], Optional[Tuple[str, Any]]]


def lens_path(
    state: FabricState,
    source: str = "",
    destination: str = "",
    ni: str = "default",
) -> List[Hop]:
    """How the fabric would forward from one place to an address, hop by hop.

    Computed from the route tables rather than probed: at each node the
    destination is looked up, the egress interface that lookup selects is
    matched against LLDP to find the node on the other end, and the walk
    continues there. ECMP is followed on every branch, so the table shows the
    whole fan-out rather than one arbitrary path through it.

    A lookup in a VRF that resolves over the overlay hands the walk back to the
    underlay: the walk switches to the default instance and continues towards
    the VTEP, which is how the two tables actually compose on the wire. When
    the VTEP is reached, the walk resumes in the original VRF on the remote
    node, which is how a DCI path traces end-to-end.

    The final hop of a delivered destination includes the ARP or ND entry for
    it, confirming the host is reachable, or noting when no binding exists.
    """
    target = _address(destination)
    if target is None:
        raise ValueError(f"'{destination}' is not an IP address")
    report = _rib_report(target)
    peers = _lldp_peers(state)
    neighbors = _neighbor_index(state)
    hops: List[Hop] = []

    pending: List[_Pending] = [
        (node, ni, target, 1, (), None) for node in _starting_nodes(state, source, ni)
    ]
    while pending:
        node, instance, address, hop, seen, resume = pending.pop(0)
        step = f"{node}/{instance}"
        here = dict(hop=hop, node=node, ni=instance, address=str(address))
        if hop > MAX_HOPS or step in seen:
            hops.append(
                Hop(**here, outcome="loop", visited=seen)
                if step in seen
                else Hop(**here, outcome="too-long")
            )
            continue

        route = _lpm(_routes(state, report, node, instance), address)
        if route is None:
            hops.append(Hop(**here, outcome="no-route"))
            continue

        kind = text(route.get("type"))
        egress = [str(i) for i in as_list(route.get("itf")) if i]
        matched = dict(
            prefix=str(route.get("Prefix", "")),
            route_type=kind,
            next_hops=tuple(str(n) for n in as_list(route.get("next-hop")) if n),
        )

        # A route that resolved over the overlay names its tunnels rather than
        # an interface. The walk hands off to the underlay there: one branch
        # per VTEP, looked up again in the default instance, which is how the
        # two route tables actually compose on the wire.
        tunnels = [m.group("vtep") for m in map(_TUNNEL.match, egress) if m]
        if tunnels:
            for vtep in tunnels:
                hops.append(
                    Hop(**here, **matched, outcome="overlay", egress=f"vxlan:{vtep}", vtep=vtep)
                )
                address_of_vtep = _address(vtep)
                if address_of_vtep is not None:
                    # Carry the VRF and destination along, so the walk can
                    # resume in the VRF once the VTEP is reached.
                    pending.append(
                        (node, "default", address_of_vtep, hop + 1, seen + (step,), (instance, address))
                    )
            continue

        if kind in _ATTACHED or not egress:
            # An attached route has one interface, or none at all when the
            # destination is the node itself.
            attached = egress or [""]
            if resume is not None:
                # The underlay delivered the VTEP: resume in the VRF on the
                # node that owns it.
                resume_ni, resume_address = resume
                for interface in attached:
                    hops.append(
                        Hop(**here, **matched, outcome="vtep-reached", egress=interface, resumes_in=resume_ni)
                    )
                pending.append((node, resume_ni, resume_address, hop + 1, seen + (step,), None))
                continue
            for interface in attached:
                hops.append(Hop(**here, **matched, outcome="delivered", egress=interface))
            if address != target:
                continue
            # The destination itself is delivered: confirm the last mile.
            last = dict(hop=hop + 1, node=node, ni=instance, address=str(target))
            if kind == "host":
                # A host route is the node's own address (a loopback, system0):
                # there is no neighbour to resolve.
                for interface in attached:
                    hops.append(Hop(**last, outcome="local-ip", egress=interface))
                continue
            bound = neighbors.get((node, str(target)), [])
            for interface, mac, origin in bound:
                hops.append(
                    Hop(**last, outcome="neighbor", egress=interface, mac=mac, origin=origin)
                )
            if not bound:
                hops.append(Hop(**last, outcome="no-neighbor"))
            continue

        for subinterface in egress:
            peer = peers.get((node, parent(subinterface)))
            if peer is None:
                hops.append(Hop(**here, **matched, outcome="dead-end", egress=subinterface))
                continue
            hops.append(
                Hop(
                    **here,
                    **matched,
                    outcome="forwarded",
                    egress=subinterface,
                    peer=peer[0],
                    peer_port=peer[1],
                )
            )
            pending.append((peer[0], instance, address, hop + 1, seen + (step,), resume))

    hops.sort(key=lambda h: (h.hop, h.node, h.egress))
    return hops


#: The sentence the Detail column makes of each outcome. Every outcome has one,
#: even where it is empty, so that a new outcome cannot render as nothing by
#: accident.
_HOP_DETAIL: Dict[str, Callable[[Hop], str]] = {
    "forwarded": lambda h: "",
    "dead-end": lambda h: (
        f"no LLDP neighbour on {parent(h.egress)}, the path stops being traceable here"
    ),
    "overlay": lambda h: f"over the overlay, continuing to VTEP {h.vtep} in default",
    "vtep-reached": lambda h: f"VTEP reached, continuing in {h.resumes_in}",
    "delivered": lambda h: f"delivered here, {h.route_type} on {h.egress or 'this node'}",
    "local-ip": lambda h: f"locally configured on {h.egress or 'this node'}",
    "neighbor": lambda h: f"{h.mac} on {h.egress}, {h.origin}",
    "no-neighbor": lambda h: f"no ARP/ND entry for {h.address} on this node",
    "no-route": lambda h: f"nothing in {h.ni} matches {h.address}",
    "loop": lambda h: f"already visited on this path ({' -> '.join(h.visited)})",
    "too-long": lambda h: f"still not delivered after {MAX_HOPS} hops",
}

#: Outcomes that are about the delivered address rather than a route, so the
#: Prefix column shows the address the walk was confirming.
_LAST_MILE = ("local-ip", "neighbor", "no-neighbor")


PATH_COLUMNS: Tuple[Column, ...] = (
    Column("Hop", "hop"),
    Column("NI", "ni"),
    Column("Prefix", lambda h: h.prefix or (h.address if h.outcome in _LAST_MILE else "-")),
    Column("Type", lambda h: h.route_type or h.outcome),
    Column("Next-hop", lambda h: h.mac or _joined(h.next_hops)),
    Column("Egress", "egress"),
    Column("Peer", lambda h: f"{h.peer} {h.peer_port}".strip()),
    Column("Detail", lambda h: _HOP_DETAIL[h.outcome](h)),
)


# --------------------------------------------------------------------------- #
# service: one service, everywhere it exists
# --------------------------------------------------------------------------- #


def lens_service(state: FabricState, name: str = "") -> List[Service]:
    """One network-instance as every node that carries it sees it.

    The services reports are a per-node projection - this leaf's view of every
    service on it. Troubleshooting a service wants the transpose: one service,
    one record per node, so that the node whose VNI or route-target does not
    match the others is a column to read down rather than four tables to
    compare.
    """
    wanted = str(name or "").strip()
    if not wanted:
        raise ValueError("service needs the name of a network-instance")
    pattern = re.compile(wanted, re.IGNORECASE)

    # vxlan-interface -> what that node sends on it.
    overlays: Dict[Tuple[str, str], Dict[str, Any]] = {
        (node, str(entry.get("vxlan-itf", ""))): entry
        for node, entry in state.items("vxlan")
    }
    macs: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for node, ni_entry, entry in state.sub_items("mac", "Fib"):
        macs.setdefault((node, str(ni_entry.get("NI", ""))), []).append(entry)
    segments: Dict[Tuple[str, str], List[str]] = {}
    for node, entry in state.items("es"):
        # ``ni-peers`` names the instances a segment is associated with.
        for instance in re.findall(r"([^:\[\],]+):\[", str(entry.get("ni-peers") or "")):
            segments.setdefault((node, instance.strip()), []).append(str(entry.get("name", "")))

    services: List[Service] = []
    for node, entry in state.items("ni"):
        instance = str(entry.get("NI", ""))
        if not pattern.search(instance):
            continue
        interfaces = [i for i in as_list(entry.get("itfs")) if isinstance(i, dict)]
        # The overlays, RTs and EVIs arrive joined, so they are read back apart.
        overlay_names = parse_listed(entry.get("vxlan-itf"))
        vnis, vteps = [], []
        for overlay in overlay_names:
            found = overlays.get((node, overlay))
            if not found:
                continue
            vnis.append(str(found.get("ing-vni", "")))
            vteps.extend(vtep for vtep, _vni in parse_vteps(found.get("destinations")))
        entries = macs.get((node, instance), [])
        local = sum(1 for e in entries if parse_destination(e.get("Dest")).local)
        services.append(
            Service(
                node=node,
                ni=instance,
                type=str(entry.get("type") or ""),
                oper=str(entry.get("oper") or ""),
                evis=parse_listed(entry.get("evi")),
                vnis=tuple(vnis),
                import_rts=parse_listed(entry.get("In-RT")),
                export_rts=parse_listed(entry.get("Out-RT")),
                interfaces=tuple(
                    Interface(name=str(i.get("Subitf") or ""), oper=text(i.get("if-oper")))
                    for i in interfaces
                ),
                bound=tuple(
                    sorted({str(i.get("assoc-ni")) for i in interfaces if i.get("assoc-ni")})
                ),
                vteps=tuple(sorted(set(vteps))),
                local_macs=local,
                remote_macs=len(entries) - local,
                segments=tuple(sorted(set(segments.get((node, instance), [])))),
            )
        )

    if not services:
        raise ValueError(
            f"no network-instance matching '{wanted}' on any of the "
            f"{len(state.nodes('ni'))} node(s) collected"
        )
    return services


SERVICE_COLUMNS: Tuple[Column, ...] = (
    Column("NI", "ni"),
    Column("Type", "type"),
    Column("Oper", "oper"),
    Column("EVI", lambda s: ", ".join(s.evis)),
    Column("VNI", lambda s: _joined(s.vnis)),
    Column("In-RT", lambda s: ", ".join(s.import_rts)),
    Column("Out-RT", lambda s: ", ".join(s.export_rts)),
    Column("Interfaces", lambda s: _joined(f"{i.name}({i.oper})" for i in s.interfaces)),
    Column("Bound", lambda s: _joined(s.bound)),
    Column("VTEPs", lambda s: _joined(s.vteps)),
    Column(
        "MACs",
        lambda s: (
            f"{s.local_macs} local / {s.remote_macs} remote"
            if s.local_macs or s.remote_macs
            else ""
        ),
    ),
    Column("ES", lambda s: _joined(s.segments)),
)


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

LENSES: Tuple[LensSpec, ...] = (
    LensSpec(
        name="where",
        title="Where",
        description=(
            "Locates a MAC or IP address across the fabric: which node owns it, "
            "which nodes learned it over the overlay, and whether more than one "
            "claims it locally."
        ),
        requires=("mac", "arp", "nd", "es"),
        columns=WHERE_COLUMNS,
        run=lens_where,
        params=(
            ParamSpec(
                name="target",
                label="Address",
                placeholder="00:C1:AB:00:01:21 or 10.0.1.51",
                help="The MAC or IP address to locate",
            ),
        ),
        mcp_name="locate_address",
    ),
    LensSpec(
        name="path",
        title="Path",
        description=(
            "Walks the route tables hop by hop from a node or address towards a "
            "destination, following every ECMP branch and handing off from a VRF "
            "to the underlay at the VTEP."
        ),
        requires=("ipv4_rib", "ipv6_rib", "lldp", "arp", "nd"),
        columns=PATH_COLUMNS,
        run=lens_path,
        params=(
            ParamSpec(
                name="source",
                label="From",
                placeholder="leaf1 or 10.0.1.51",
                help="The node or attached address the walk starts from",
            ),
            ParamSpec(
                name="destination",
                label="To",
                placeholder="10.0.2.51",
                help="The address being forwarded towards",
                kind="address",
            ),
            ParamSpec(
                name="ni",
                label="Network instance",
                placeholder="default",
                help="The instance to look the destination up in",
            ),
        ),
        mcp_name="trace_path",
        group_by_node=False,
    ),
    LensSpec(
        name="service",
        title="Service",
        description=(
            "One network-instance as every node that carries it sees it: type, "
            "EVI, VNI, route-targets, bound interfaces, VTEP peers, MAC counts "
            "and ethernet-segments, one row per node."
        ),
        requires=("ni", "vxlan", "mac", "es"),
        columns=SERVICE_COLUMNS,
        run=lens_service,
        params=(
            ParamSpec(
                name="name",
                label="Service",
                placeholder="subnet-1",
                help="Network-instance name, matched as a case-insensitive regex",
            ),
        ),
        mcp_name="service_detail",
    ),
)

LENSES_BY_NAME: Dict[str, LensSpec] = {lens.name: lens for lens in LENSES}

#: Every report the lenses read, which is what a surface may have to collect.
LENS_REPORTS: Tuple[str, ...] = tuple(
    dict.fromkeys(report for lens in LENSES for report in lens.requires)
)


def get_lens(name: str) -> LensSpec:
    """Look a lens up by its canonical name."""
    try:
        return LENSES_BY_NAME[name.replace("-", "_")]
    except KeyError:
        raise KeyError(f"unknown lens '{name}'") from None


def lenses_for(surface: str) -> List[LensSpec]:
    """Every lens offered on *surface*, in registry order."""
    return [lens for lens in LENSES if lens.on(surface)]


__all__ = [
    "LENSES",
    "LENSES_BY_NAME",
    "LENS_REPORTS",
    "PATH_COLUMNS",
    "SERVICE_COLUMNS",
    "WHERE_COLUMNS",
    "Column",
    "Destination",
    "Hop",
    "Interface",
    "LensSpec",
    "Service",
    "Sighting",
    "as_dict",
    "get_lens",
    "lens_path",
    "lens_service",
    "lens_where",
    "lenses_for",
    "parse_destination",
    "parse_listed",
    "parse_vteps",
]
