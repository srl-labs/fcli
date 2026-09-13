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
rows the way both do. :data:`LENSES` is the single registry, so a lens is
defined once and the CLI command and the MCP tool are generated from it.

Adding one means writing a function over a :class:`FabricState`, declaring the
reports it reads, and listing it in :data:`LENSES`.

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
from dataclasses import dataclass
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
)

from .aliases import resolve
from .fabric import FabricState, as_list, out_of_band, parent, text
from .reports import INTERACTIVE, ParamSpec

#: How far a path walk follows the fabric before deciding it is going in
#: circles. A datacenter fabric is three tiers; anything beyond this is a loop
#: the walk should report rather than keep following.
MAX_HOPS = 16


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
    #: Columns, in the order they read. ``Node`` is rendered by the table
    #: itself, so it is not repeated here.
    columns: Tuple[str, ...]
    #: Called as ``run(state, **params)``.
    run: Callable[..., List[Dict[str, Any]]]
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

    def on(self, surface: str) -> bool:
        return surface in self.surfaces


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


def lens_where(state: FabricState, target: str = "") -> List[Dict[str, Any]]:
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

    rows: List[Dict[str, Any]] = []
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
            rows.append(
                {
                    "Node": node,
                    "NI": itf.get("NI", ""),
                    "Found": "arp" if entry["_report"] == "arp" else "neighbor",
                    "Address": str(address),
                    "Via": itf.get("interface", ""),
                    "Detail": f"{bound or entry.get('MAC', '?')}, {text(entry.get('Type'))}"
                    + (f", expires {entry.get('expiry')}" if entry.get("expiry") else ""),
                }
            )
        if not mac:
            return rows

    segments = _es_by_esi(state)
    local_owners: Dict[str, List[Tuple[str, str, Destination]]] = {}
    for node, ni_entry, entry in state.sub_items("mac", "Fib"):
        if _mac(entry.get("Address")) != mac:
            continue
        ni = str(ni_entry.get("NI", ""))
        # The destination arrives pre-formatted, so it is read back apart here.
        dest = parse_destination(entry.get("Dest"))
        detail = text(entry.get("Type"))
        if dest.kind == "vtep":
            detail += f", overlay {dest.overlay}" + (f", vni {dest.vni}" if dest.vni else "")
        elif dest.kind == "esi":
            named = ", ".join(
                sorted({str(es.get("name")) for _n, es in segments.get(dest.via, [])})
            )
            detail += f", segment {named or 'not local'}"
        if dest.local:
            local_owners.setdefault(ni, []).append((node, mac, dest))
        rows.append(
            {
                "Node": node,
                "NI": ni,
                "Found": "local" if dest.local else "remote",
                "Address": mac,
                "Via": dest.via,
                "Detail": detail,
            }
        )

    # Two nodes both owning one MAC locally is legitimate when they are the two
    # sides of an all-active segment, and is a duplicate or a silent move
    # otherwise. The bridge table cannot say which, so say that it cannot.
    for ni, owners in sorted(local_owners.items()):
        if len(owners) < 2:
            continue
        nodes = sorted({node for node, _m, _d in owners})
        if len(nodes) < 2:
            continue
        rows.append(
            {
                "Node": ", ".join(nodes),
                "NI": ni,
                "Found": "duplicate",
                "Address": mac,
                "Via": _joined(sorted({d.via for _n, _m, d in owners})),
                "Detail": (
                    "learned locally on more than one node: expected on an all-active "
                    "segment, a move or a duplicate otherwise"
                ),
            }
        )

    if not rows:
        rows.append(
            {
                "Node": "-",
                "NI": "-",
                "Found": "not found",
                "Address": mac or wanted,
                "Via": "",
                "Detail": f"no node reports it in any bridge table ({len(state.nodes('mac'))} searched)",
            }
        )
    return rows


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


def lens_path(
    state: FabricState,
    source: str = "",
    destination: str = "",
    ni: str = "default",
) -> List[Dict[str, Any]]:
    """How the fabric would forward from one place to an address, hop by hop.

    Computed from the route tables rather than probed: at each node the
    destination is looked up, the egress interface that lookup selects is
    matched against LLDP to find the node on the other end, and the walk
    continues there. ECMP is followed on every branch, so the table shows the
    whole fan-out rather than one arbitrary path through it.

    A lookup in a VRF that resolves over the overlay hands the walk back to the
    underlay: the walk switches to the default instance and continues towards
    the VTEP, which is how the two tables actually compose on the wire.
    """
    target = _address(destination)
    if target is None:
        raise ValueError(f"'{destination}' is not an IP address")
    report = _rib_report(target)
    peers = _lldp_peers(state)
    rows: List[Dict[str, Any]] = []

    # (node, network-instance, address being looked up), and the hop it is on.
    pending: List[Tuple[str, str, Any, int, Tuple[str, ...]]] = [
        (node, ni, target, 1, ()) for node in _starting_nodes(state, source, ni)
    ]
    while pending:
        node, instance, address, hop, seen = pending.pop(0)
        step = f"{node}/{instance}"
        if hop > MAX_HOPS or step in seen:
            rows.append(
                {
                    "Hop": hop,
                    "Node": node,
                    "NI": instance,
                    "Prefix": "-",
                    "Type": "loop" if step in seen else "too long",
                    "Next-hop": "",
                    "Egress": "",
                    "Peer": "",
                    "Detail": (
                        f"already visited on this path ({' -> '.join(seen)})"
                        if step in seen
                        else f"still not delivered after {MAX_HOPS} hops"
                    ),
                }
            )
            continue

        route = _lpm(_routes(state, report, node, instance), address)
        if route is None:
            rows.append(
                {
                    "Hop": hop,
                    "Node": node,
                    "NI": instance,
                    "Prefix": "-",
                    "Type": "no route",
                    "Next-hop": "",
                    "Egress": "",
                    "Peer": "",
                    "Detail": f"nothing in {instance} matches {address}",
                }
            )
            continue

        kind = text(route.get("type"))
        egress = [str(i) for i in as_list(route.get("itf")) if i]
        next_hops = [str(n) for n in as_list(route.get("next-hop")) if n]
        base = {
            "Hop": hop,
            "Node": node,
            "NI": instance,
            "Prefix": str(route.get("Prefix", "")),
            "Type": kind,
            "Next-hop": _joined(next_hops),
        }

        # A route that resolved over the overlay names its tunnels rather than
        # an interface. The walk hands off to the underlay there: one branch
        # per VTEP, looked up again in the default instance, which is how the
        # two route tables actually compose on the wire.
        tunnels = [m.group("vtep") for m in map(_TUNNEL.match, egress) if m]
        if tunnels:
            for vtep in tunnels:
                rows.append(
                    {
                        **base,
                        "Egress": f"vxlan:{vtep}",
                        "Peer": "",
                        "Detail": f"over the overlay, continuing to VTEP {vtep} in default",
                    }
                )
                address_of_vtep = _address(vtep)
                if address_of_vtep is not None:
                    pending.append(
                        (node, "default", address_of_vtep, hop + 1, seen + (step,))
                    )
            continue

        if kind in _ATTACHED or not egress:
            rows.append(
                {
                    **base,
                    "Egress": _joined(egress),
                    "Peer": "",
                    "Detail": f"delivered here, {kind} on {_joined(egress) or 'this node'}",
                }
            )
            continue

        for subinterface in egress:
            port = parent(subinterface)
            peer = peers.get((node, port))
            rows.append(
                {
                    **base,
                    "Egress": subinterface,
                    "Peer": f"{peer[0]} {peer[1]}".strip() if peer else "",
                    "Detail": (
                        ""
                        if peer
                        else f"no LLDP neighbour on {port}, the path stops being traceable here"
                    ),
                }
            )
            if peer:
                pending.append((peer[0], instance, address, hop + 1, seen + (step,)))

    rows.sort(key=lambda r: (r["Hop"], r["Node"], str(r.get("Egress", ""))))
    return rows


# --------------------------------------------------------------------------- #
# service: one service, everywhere it exists
# --------------------------------------------------------------------------- #


def lens_service(state: FabricState, name: str = "") -> List[Dict[str, Any]]:
    """One network-instance as every node that carries it sees it.

    The services reports are a per-node projection - this leaf's view of every
    service on it. Troubleshooting a service wants the transpose: one service,
    one row per node, so that the node whose VNI or route-target does not match
    the others is a column to read down rather than four tables to compare.
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

    rows: List[Dict[str, Any]] = []
    for node, entry in state.items("ni"):
        instance = str(entry.get("NI", ""))
        if not pattern.search(instance):
            continue
        interfaces = [i for i in as_list(entry.get("itfs")) if isinstance(i, dict)]
        overlay_names = [
            i.strip() for i in str(entry.get("vxlan-itf") or "").split(",") if i.strip()
        ]
        vnis, vteps = [], []
        for overlay in overlay_names:
            found = overlays.get((node, overlay))
            if not found:
                continue
            vnis.append(str(found.get("ing-vni", "")))
            vteps.extend(vtep for vtep, _vni in parse_vteps(found.get("destinations")))
        entries = macs.get((node, instance), [])
        local = sum(1 for e in entries if parse_destination(e.get("Dest")).local)
        rows.append(
            {
                "Node": node,
                "NI": instance,
                "Type": entry.get("type", ""),
                "Oper": entry.get("oper", ""),
                "EVI": entry.get("evi", ""),
                "VNI": _joined(vnis),
                "In-RT": entry.get("In-RT", ""),
                "Out-RT": entry.get("Out-RT", ""),
                "Interfaces": _joined(
                    f"{i.get('Subitf')}({text(i.get('if-oper'))})" for i in interfaces
                ),
                "Bound": _joined(
                    sorted({str(i.get("assoc-ni")) for i in interfaces if i.get("assoc-ni")})
                ),
                "VTEPs": _joined(sorted(set(vteps))),
                "MACs": f"{local} local / {len(entries) - local} remote" if entries else "",
                "ES": _joined(sorted(set(segments.get((node, instance), [])))),
            }
        )

    if not rows:
        raise ValueError(
            f"no network-instance matching '{wanted}' on any of the "
            f"{len(state.nodes('ni'))} node(s) collected"
        )
    return rows


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
        columns=("NI", "Found", "Address", "Via", "Detail"),
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
        columns=("Hop", "NI", "Prefix", "Type", "Next-hop", "Egress", "Peer", "Detail"),
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
        columns=(
            "NI",
            "Type",
            "Oper",
            "EVI",
            "VNI",
            "In-RT",
            "Out-RT",
            "Interfaces",
            "Bound",
            "VTEPs",
            "MACs",
            "ES",
        ),
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
    "Destination",
    "LensSpec",
    "get_lens",
    "lens_path",
    "lens_service",
    "lens_where",
    "lenses_for",
    "parse_destination",
    "parse_vteps",
]
