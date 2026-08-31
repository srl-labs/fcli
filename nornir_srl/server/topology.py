"""The fabric topology, inferred from LLDP adjacencies and where services live.

Nothing tells fcli what a node is. The inventory may carry role labels or
nothing at all, and a label is a claim about intent rather than about what the
node runs. So the tier of a node is derived from the two things the fabric
itself reports: the services configured on it, and who its LLDP neighbours are.

* A node with mac-vrfs, and optionally ip-vrfs, is a **leaf**: the tier where a
  service meets a port.
* A node whose services carry two or more enabled ``bgp-vpn`` instances is a
  **DCGW**. The second instance is the WAN side of a stitched service, which
  only a gateway out of the DC has.
* A node with no mac-vrf and no ip-vrf that sees two or more leaves is a
  **spine**: it interconnects the leaves without terminating anything.
* Any other node without services is **core**: WAN P/PE routers and
  super-spines, which transit the fabric but attach to no leaf of it.

Below all of them sit the **clients**, which are not nodes of the inventory at
all but the far end of what a service is configured towards: a bridged
subinterface of a mac-vrf, or a routed port of an ip-vrf, on a port that faces
nothing else we know of. A client is identified by the name it advertises over
LLDP, or failing that by the ESI of the ethernet-segment its port is in, so that
a multi-homed one is a single box spanning its leaves.

Between the two sit the **ethernet-segments**. A multi-homed client does not
reach its leaves over a cable each but over one bundle, which is a configured
object in its own right: it has a name, an ESI that every leaf on it agrees on,
and it is where multi-homing goes wrong. So it is drawn as a tier of its own,
and the client hangs off the bundle rather than off each leaf.

The result is drawn one tier per layer, clients at the bottom and the WAN on top,
and is split into **fabrics**: nodes that share no cable with each other are not
one topology, however many clients happen to be plugged into both of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

#: The tiers of a fabric, bottom up, as ``(layer, role, label)``.
LAYERS: Tuple[Tuple[int, str, str], ...] = (
    (0, "client", "Clients"),
    (1, "segment", "Ethernet segments"),
    (2, "edge", "Edge / unclassified"),
    (3, "leaf", "Leaves"),
    (4, "spine", "Spines"),
    (5, "dcgw", "DC gateways"),
    (6, "core", "WAN / core"),
)

#: What an ethernet-segment node is named, so that it cannot collide with the
#: client on it - which, having nothing else to go by, is named after the ESI.
_SEGMENT_PREFIX = "es:"

#: Roles that share the layer holding whatever we could not place: nodes that
#: have told us nothing yet, and nodes we only know from someone's LLDP.
_EDGE_ROLES = ("unknown", "external")

#: Roles that are not a node of the fabric but something drawn from what a node
#: is configured towards, and that therefore cannot join two fabrics into one.
_ATTACHED_ROLES = ("client", "segment")

_LAYER_OF = {role: layer for layer, role, _ in LAYERS}
_LAYER_OF.update({role: _LAYER_OF["edge"] for role in _EDGE_ROLES})
_LAYER_LABEL = {layer: label for layer, _, label in LAYERS}

_UP_STATES = frozenset({"up", "enable", "enabled", "active"})
_DOWN_STATES = frozenset({"down", "disable", "disabled"})

#: Ports whose LLDP says nothing about the fabric. Every node of a lab hangs off
#: the same management bridge and sees all the others on it, which would draw a
#: full mesh over the topology and leave every node with the same neighbours.
_OUT_OF_BAND = ("mgmt",)

#: Interfaces a service binds that no client can be on. An IRB is the routed
#: side of a bridge domain, a loopback and ``system0`` are the node's own
#: addresses, and the management port is out of band.
_VIRTUAL_PORTS = ("irb", "lo", "system", "mgmt")


@dataclass
class Adjacency:
    """One LLDP neighbour, seen on one local port."""

    local_port: str
    #: The system-name the neighbour advertises, which is not the name the
    #: inventory knows it by.
    peer: str
    peer_port: str = ""
    oper_state: str = ""


@dataclass
class Segment:
    """An ethernet-segment: one cable bundle, seen from each node it lands on.

    The ESI is the same on every node a multi-homed client is attached to, and
    for a client that runs no LLDP it is the only thing that says the two lags
    are one host rather than two.
    """

    name: str
    esi: str


@dataclass
class Attachment:
    """One client-facing subinterface: where a service meets a customer port."""

    subinterface: str
    #: The port the subinterface is on. LLDP reports a neighbour against the
    #: port, and it is what makes several vlans of one cable a single client.
    port: str
    #: The network-instance the subinterface is bound to.
    service: str
    #: ``bridged`` for a member of a mac-vrf, ``routed`` for one of an ip-vrf.
    kind: str
    vlan: str = ""
    ip: str = ""
    oper_state: str = ""


@dataclass
class NodeFacts:
    """What one node contributes to the topology."""

    name: str
    hostname: str = ""
    system_name: str = ""
    site: str = ""
    mac_vrfs: int = 0
    ip_vrfs: int = 0
    #: Services with two or more enabled ``bgp-vpn`` instances: the DCGW mark.
    stitched: int = 0
    #: Whether any network-instance state arrived at all, which is what
    #: separates 'runs no services' from 'has not told us anything yet'.
    has_state: bool = False
    connected: bool = True
    error: Optional[str] = None
    adjacencies: List[Adjacency] = field(default_factory=list)
    #: The service-carrying subinterfaces, before the fabric-facing ones are
    #: taken out of them - which needs every node's LLDP, so it happens in
    #: :func:`build_topology` rather than here.
    attachments: List[Attachment] = field(default_factory=list)
    #: The ethernet-segment on each port that is in one.
    segments: Dict[str, Segment] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """The short name to draw the node under."""
        return self.system_name or self.name


def node_facts(
    name: str,
    *,
    hostname: str = "",
    labels: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    connected: bool = True,
    error: Optional[str] = None,
) -> NodeFacts:
    """Read one node's contribution out of its streamed state.

    *snapshot* is the ``system``, ``network-instance`` and ``interface`` trees as
    :meth:`~nornir_srl.server.stream.HostStream.snapshot_roots` returns them. A
    node with nothing streamed yet yields facts that classify as ``unknown``
    rather than as a node without services.
    """
    labels = labels or {}
    snapshot = snapshot or {}
    system = _branch(snapshot, "system")
    facts = NodeFacts(
        name=name,
        hostname=hostname or name,
        system_name=str(_branch(system, "name").get("host-name") or ""),
        site=str(labels.get("site") or ""),
        connected=connected,
        error=error,
    )

    instances = snapshot.get("network-instance")
    if isinstance(instances, list) and instances:
        facts.has_state = True
        details = _subinterface_details(snapshot)
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            ni_type = _norm(instance.get("type"))
            if ni_type == "mac-vrf":
                facts.mac_vrfs += 1
                kind = "bridged"
            elif ni_type in ("ip-vrf", "vrf") and str(instance.get("name", "")) != "mgmt":
                facts.ip_vrfs += 1
                kind = "routed"
            else:
                continue
            facts.attachments.extend(_attachments(instance, kind, details))
            if _is_stitched(instance):
                facts.stitched += 1

    facts.adjacencies = _adjacencies(system, _interface_states(snapshot))
    facts.segments = _segments(system)
    return facts


def build_topology(facts: Iterable[NodeFacts]) -> Dict[str, Any]:
    """Turn per-node facts into the nodes, links and layers of the fabric."""
    nodes = list(facts)
    aliases = _alias_index(nodes)

    peers: Dict[str, Set[str]] = {f.name: set() for f in nodes}
    #: Neighbours that match no node of the inventory, by advertised name.
    outside: Dict[str, Set[str]] = {}
    links: Dict[Tuple[str, str], Dict[str, Any]] = {}
    #: The local ports that face a node of the inventory: the fabric itself.
    fabric_ports: Dict[str, Set[str]] = {f.name: set() for f in nodes}
    #: What an unmatched neighbour calls itself, per port it was seen on.
    outside_ports: Dict[Tuple[str, str], str] = {}

    for node in nodes:
        for adj in node.adjacencies:
            target = _resolve(adj.peer, aliases)
            if target == node.name:  # a neighbour on our own name is a loop
                continue
            if target is None:
                target = adj.peer
                outside.setdefault(target, set()).add(node.name)
                outside_ports[(node.name, adj.local_port)] = target
            else:
                fabric_ports[node.name].add(adj.local_port)
                peers[node.name].add(target)
                peers.setdefault(target, set()).add(node.name)
            _record_link(links, node.name, target, adj)

    # Roles are settled before the clients are added, so that hanging one off a
    # node can never be what decides which tier the node itself is on.
    roles = _classify(nodes, peers)
    clients = _clients(nodes, fabric_ports, outside_ports)
    segments = _segment_nodes(clients)
    for client in clients:
        #: The state of every port of a bundle, per ethernet-segment node.
        bundles: Dict[str, List[str]] = {}
        for attachment in client["attachments"]:
            # A port that is in an ethernet-segment is cabled to the segment
            # rather than to the client, and the segment down to the client.
            near = _segment_key(attachment["esi"]) if attachment["esi"] else client["name"]
            peers[attachment["node"]].add(near)
            _record_access_link(links, near, attachment)
            if attachment["esi"]:
                bundles.setdefault(near, []).append(attachment["state"])
        for name, states in sorted(bundles.items()):
            _record_bundle_link(links, name, client["name"], states)
        # A client that named itself over LLDP is no longer a stray neighbour.
        outside.pop(client["name"], None)

    counts = _client_counts(clients)
    payload = [
        _node_payload(node, roles[node.name], sorted(peers[node.name]), counts.get(node.name, 0))
        for node in nodes
    ]
    payload.extend(
        _external_payload(name, sorted(seen_by)) for name, seen_by in sorted(outside.items())
    )
    payload.extend(segments)
    payload.extend(clients)
    payload.sort(key=lambda n: (-n["layer"], n["site"], _row_order(n), n["label"]))
    layer_of = {node["name"]: node["layer"] for node in payload}
    cables = [_link_payload(link, layer_of) for _key, link in sorted(links.items())]

    return {
        "nodes": payload,
        "links": cables,
        # Top of the drawing first, and only the tiers this fabric actually has.
        "layers": _layers(payload),
        "roles": _role_counts(payload),
        "fabrics": _assign_fabrics(payload, cables),
        "sites": sorted({n["site"] for n in payload if n["site"]}),
        "unresolved": [
            {"peer": name, "seen_by": sorted(seen_by)} for name, seen_by in sorted(outside.items())
        ],
    }


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def _classify(nodes: List[NodeFacts], peers: Dict[str, Set[str]]) -> Dict[str, str]:
    """Assign a role to every node, services first and adjacency second."""
    roles: Dict[str, str] = {}
    for node in nodes:
        if node.mac_vrfs or node.ip_vrfs:
            roles[node.name] = "dcgw" if node.stitched else "leaf"
        elif not node.has_state:
            roles[node.name] = "unknown"

    leaves = {name for name, role in roles.items() if role == "leaf"}
    gateways = {name for name, role in roles.items() if role == "dcgw"}
    for node in nodes:
        if node.name in roles:
            continue
        attached = peers.get(node.name, set())
        leaf_peers = len(attached & leaves)
        # Two leaves is what a spine is for: interconnecting them. A single leaf
        # counts as well, for a fabric small enough to have only one, but not
        # when the node also faces a gateway - that is a WAN router hanging off
        # the fabric rather than a spine inside it.
        if leaf_peers >= 2 or (leaf_peers == 1 and not attached & gateways):
            roles[node.name] = "spine"
        else:
            roles[node.name] = "core"
    return roles


def _node_payload(node: NodeFacts, role: str, peers: List[str], clients: int) -> Dict[str, Any]:
    return {
        "name": node.name,
        "label": node.label,
        "role": role,
        "layer": _LAYER_OF.get(role, _LAYER_OF["edge"]),
        "site": node.site,
        "mac_vrfs": node.mac_vrfs,
        "ip_vrfs": node.ip_vrfs,
        "stitched": node.stitched,
        "clients": clients,
        "peers": peers,
        "ports": len(node.adjacencies),
        "connected": node.connected,
        "error": node.error,
        "external": False,
        "attachments": [],
    }


def _external_payload(name: str, seen_by: List[str]) -> Dict[str, Any]:
    """A node we only know because a neighbour of ours advertises it."""
    return {
        "name": name,
        "label": name,
        "role": "external",
        "layer": _LAYER_OF["external"],
        "site": "",
        "mac_vrfs": 0,
        "ip_vrfs": 0,
        "stitched": 0,
        "clients": 0,
        "peers": seen_by,
        "ports": len(seen_by),
        "connected": True,
        "error": None,
        "external": True,
        "attachments": [],
    }


def _row_order(node: Dict[str, Any]) -> str:
    """Clients and segments are drawn under a node they attach to, not by name."""
    if node["attachments"]:
        return min(attachment["node"] for attachment in node["attachments"])
    return ""


def _layers(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The occupied tiers, top of the drawing first."""
    result = []
    for layer in sorted({n["layer"] for n in nodes}, reverse=True):
        members = [n["name"] for n in nodes if n["layer"] == layer]
        result.append(
            {
                "index": layer,
                "label": _LAYER_LABEL.get(layer, f"Layer {layer}"),
                "nodes": members,
            }
        )
    return result


def _role_counts(nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for node in nodes:
        counts[node["role"]] = counts.get(node["role"], 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# fabrics
# --------------------------------------------------------------------------- #


def _assign_fabrics(
    nodes: List[Dict[str, Any]], links: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Split the drawing into the fabrics that share no cable between nodes.

    Two nodes that meet only through a client are not one fabric. A server
    plugged into two pods, or into a DC and the one beside it, says nothing
    about the underlay of either, and drawing them as one topology claims a
    path across that does not exist. So the split is made on the cables between
    nodes alone, and a client is then drawn in every fabric it attaches to.

    A node with no such cable at all - one whose LLDP has not arrived yet, or
    one genuinely patched to nothing - is not a fabric of its own; those are
    gathered into a single group. Unless there are no cables anywhere, in which
    case they are the fabric rather than the leftovers of one, which is how a
    freshly started server draws one topology instead of one per node.

    Stamps ``fabrics`` on every node and returns the groups, largest first.
    """
    adjacency: Dict[str, Set[str]] = {
        node["name"]: set() for node in nodes if node["role"] not in _ATTACHED_ROLES
    }
    for link in links:
        if link["access"]:
            continue
        if link["a"] in adjacency and link["b"] in adjacency:
            adjacency[link["a"]].add(link["b"])
            adjacency[link["b"]].add(link["a"])

    components = _components(adjacency)
    cabled = sorted(
        (members for members in components if len(members) > 1),
        key=lambda members: (-len(members), members[0]),
    )
    loose = sorted(name for members in components if len(members) == 1 for name in members)
    groups: List[Tuple[List[str], bool]] = [(members, False) for members in cabled]
    if loose:
        groups.append((loose, bool(cabled)))

    fabric_of = {name: members[0] for members, _loose in groups for name in members}
    rank_of = {members[0]: rank for rank, (members, _loose) in enumerate(groups)}
    for node in nodes:
        if node["role"] in _ATTACHED_ROLES:
            found = {fabric_of[a["node"]] for a in node["attachments"] if a["node"] in fabric_of}
        else:
            found = {fabric_of[node["name"]]}
        node["fabrics"] = sorted(found, key=lambda fabric: rank_of[fabric])

    counts: Dict[str, int] = {}
    for node in nodes:
        for fabric in node["fabrics"]:
            counts[fabric] = counts.get(fabric, 0) + 1
    labels = _fabric_labels(groups, {node["name"]: node for node in nodes})
    return [
        {
            "id": members[0],
            "label": label,
            "nodes": counts.get(members[0], 0),
            "devices": len(members),
        }
        for (members, _loose), label in zip(groups, labels)
    ]


def _components(adjacency: Dict[str, Set[str]]) -> List[List[str]]:
    """The connected components of *adjacency*, each sorted by name."""
    seen: Set[str] = set()
    result = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        members: Set[str] = set()
        stack = [start]
        while stack:
            name = stack.pop()
            if name in members:
                continue
            members.add(name)
            seen.add(name)
            stack.extend(adjacency[name] - members)
        result.append(sorted(members))
    return result


def _fabric_labels(
    groups: List[Tuple[List[str], bool]], by_name: Dict[str, Dict[str, Any]]
) -> List[str]:
    """Name every fabric the same way, or none of them that way.

    A fabric is best named after itself: the site its nodes are labelled with,
    or the name they share, says what ``Fabric 2`` cannot. But a scheme that
    fits one fabric and not the next reads as though the two were different
    kinds of thing - ``frontend`` beside ``Fabric 1`` looks like a name beside a
    placeholder - so a scheme is only used when it names every fabric of the
    drawing, and distinctly. Failing that they are numbered, largest first.
    """
    cabled = [members for members, loose in groups if not loose]
    for naming in (_fabric_site, _fabric_shared_name):
        names = [naming(members, by_name) for members in cabled]
        if all(names) and len(set(names)) == len(names):
            found = iter(names)
            return ["Unattached" if loose else next(found) for _members, loose in groups]
    return [
        "Unattached" if loose else f"Fabric {rank}"
        for rank, (_members, loose) in enumerate(groups, start=1)
    ]


def _fabric_site(members: List[str], by_name: Dict[str, Dict[str, Any]]) -> str:
    """The site every node of the fabric is labelled with, if they agree on one."""
    sites = {by_name[name]["site"] for name in members}
    return next(iter(sites)) if len(sites) == 1 else ""


def _fabric_shared_name(members: List[str], by_name: Dict[str, Dict[str, Any]]) -> str:
    """The dash-separated head every node shares: ``frontend-leaf1`` is ``frontend``.

    Only a head shorter than the shortest name counts, so nodes that agree on
    their whole name do not name the fabric after themselves.
    """
    parts = [by_name[name]["label"].split("-") for name in members]
    shared: List[str] = []
    for index in range(min(len(name) for name in parts) - 1):
        segment = parts[0][index]
        if any(name[index] != segment for name in parts):
            break
        shared.append(segment)
    return "-".join(shared)


# --------------------------------------------------------------------------- #
# clients
# --------------------------------------------------------------------------- #


def _clients(
    nodes: List[NodeFacts],
    fabric_ports: Dict[str, Set[str]],
    outside_ports: Dict[Tuple[str, str], str],
) -> List[Dict[str, Any]]:
    """The clients hanging off the fabric, as nodes of their own.

    An attachment on a port that faces a node of the inventory is not a client
    but a fabric link that happens to carry a service - the WAN subinterface of
    a stitched ip-vrf on a DCGW is the obvious one - so those are left out.

    What remains is grouped per client rather than per subinterface, and several
    vlans of one cable are one client. Which ports belong to the same client is
    answered by whichever of these the fabric can tell us, in that order:

    * the name an unmatched LLDP neighbour advertises, because a client that
      says who it is says the same thing to every leaf it is attached to;
    * the ESI of the ethernet-segment the port is in, which is how a multi-homed
      client that runs no LLDP is still drawn as one box rather than one per
      leaf;
    * the port itself, when there is nothing else to go on.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        for attachment in node.attachments:
            if attachment.port in fabric_ports.get(node.name, ()):
                continue
            advertised = outside_ports.get((node.name, attachment.port))
            segment = node.segments.get(attachment.port)
            name = advertised or (segment.esi if segment else "") or f"{node.name}:{attachment.port}"
            client = grouped.setdefault(
                name,
                {
                    "name": name,
                    "advertised": advertised or "",
                    "sites": set(),
                    "attachments": [],
                },
            )
            client["sites"].add(node.site)
            client["attachments"].append(
                {
                    "node": node.name,
                    "site": node.site,
                    "port": attachment.port,
                    "subinterface": attachment.subinterface,
                    "service": attachment.service,
                    "kind": attachment.kind,
                    "vlan": attachment.vlan,
                    "ip": attachment.ip,
                    "state": attachment.oper_state,
                    "esi": segment.esi if segment else "",
                    "segment": segment.name if segment else "",
                }
            )
    return [_client_payload(client) for _name, client in sorted(grouped.items())]


def _client_payload(client: Dict[str, Any]) -> Dict[str, Any]:
    attachments = sorted(
        client["attachments"], key=lambda a: (a["node"], a["port"], a["subinterface"])
    )
    sites = {site for site in client["sites"] if site}
    esis = {a["esi"] for a in attachments if a["esi"]}
    return {
        "name": client["name"],
        # Every client box reads the same. What a client is called is inferred
        # from an ESI or a port name, which reads like an identity it does not
        # have; where it is plugged in and what it carries is in the panel.
        "label": "client",
        #: The name it advertised over LLDP, on the rare occasion it runs any.
        "advertised": client["advertised"],
        "role": "client",
        "layer": _LAYER_OF["client"],
        # A client of one site is drawn with it; one spanning two belongs to
        # neither, and grouping it under either would be a claim we cannot make.
        "site": sites.pop() if len(sites) == 1 else "",
        "mac_vrfs": 0,
        "ip_vrfs": 0,
        "stitched": 0,
        "clients": 0,
        # A port in an ethernet-segment reaches the client through it.
        "peers": sorted(
            {_segment_key(a["esi"]) if a["esi"] else a["node"] for a in attachments}
        ),
        "ports": len({(a["node"], a["port"]) for a in attachments}),
        "connected": True,
        "error": None,
        "external": False,
        "attachments": attachments,
        "services": sorted({a["service"] for a in attachments if a["service"]}),
        "esi": esis.pop() if len(esis) == 1 else "",
    }


def _client_counts(clients: List[Dict[str, Any]]) -> Dict[str, int]:
    """How many clients hang off each node, segment in between or not."""
    counts: Dict[str, int] = {}
    for client in clients:
        for name in {a["node"] for a in client["attachments"]}:
            counts[name] = counts.get(name, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# ethernet-segments
# --------------------------------------------------------------------------- #


def _segment_key(esi: str) -> str:
    """The node name of the ethernet-segment with this ESI."""
    return f"{_SEGMENT_PREFIX}{esi}"


def _segment_nodes(clients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ethernet-segments the clients are attached over, as nodes of their own.

    Keyed on the ESI rather than on the name, because the ESI is the only thing
    the leaves of one segment are made to agree on: each configures a name of
    its own, and two leaves that disagree on it are still one segment.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for client in clients:
        for attachment in client["attachments"]:
            if not attachment["esi"]:
                continue
            segment = grouped.setdefault(
                attachment["esi"],
                {
                    "esi": attachment["esi"],
                    "names": set(),
                    "sites": set(),
                    "clients": set(),
                    "attachments": [],
                },
            )
            segment["names"].add(attachment["segment"])
            segment["sites"].add(attachment["site"])
            segment["clients"].add(client["name"])
            segment["attachments"].append(attachment)
    return [_segment_payload(segment) for _esi, segment in sorted(grouped.items())]


def _segment_payload(segment: Dict[str, Any]) -> Dict[str, Any]:
    attachments = sorted(
        segment["attachments"], key=lambda a: (a["node"], a["port"], a["subinterface"])
    )
    names = {name for name in segment["names"] if name}
    sites = {site for site in segment["sites"] if site}
    clients = sorted(segment["clients"])
    return {
        "name": _segment_key(segment["esi"]),
        "label": _segment_label(segment["esi"]),
        #: The name each leaf configured it under, which they may disagree on.
        "names": sorted(names),
        "role": "segment",
        "layer": _LAYER_OF["segment"],
        "site": sites.pop() if len(sites) == 1 else "",
        "mac_vrfs": 0,
        "ip_vrfs": 0,
        "stitched": 0,
        "clients": len(clients),
        "peers": sorted({a["node"] for a in attachments} | set(clients)),
        "ports": len({(a["node"], a["port"]) for a in attachments}),
        "connected": True,
        "error": None,
        "external": False,
        "attachments": attachments,
        "services": sorted({a["service"] for a in attachments if a["service"]}),
        "esi": segment["esi"],
    }


def _segment_label(esi: str) -> str:
    """What an ethernet-segment box reads: ``ES`` and the tail of its ESI.

    The tail rather than the name, because the name is configured per leaf and
    nothing makes the two leaves of one segment agree on it, while the ESI is
    the one thing they are made to agree on. The last two bytes are what tells
    the segments of a fabric apart; the rest of an ESI is shared boilerplate.
    """
    return f"ES {':'.join(esi.split(':')[-2:])}"


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def _record_link(
    links: Dict[Tuple[str, str], Dict[str, Any]],
    node: str,
    target: str,
    adj: Adjacency,
) -> None:
    """Add one adjacency to the link it belongs to.

    Both ends of a cable report it, so links and the ports of a link are keyed
    on the pair rather than appended, and a fabric of n cables stays n links
    however many nodes are streaming.
    """
    key = (node, target) if node <= target else (target, node)
    link = links.setdefault(key, {"a": key[0], "b": key[1], "ports": {}, "states": set()})
    if node == key[0]:
        pair = (adj.local_port, adj.peer_port)
    else:
        pair = (adj.peer_port, adj.local_port)
    link["ports"].setdefault(pair, {"a_port": pair[0], "b_port": pair[1]})
    if adj.oper_state:
        link["states"].add(adj.oper_state)


def _record_access_link(
    links: Dict[Tuple[str, str], Dict[str, Any]],
    client: str,
    attachment: Dict[str, Any],
) -> None:
    """Add the cable from a node to a client hanging off one of its ports.

    A client that advertises itself over LLDP already has this cable, reported
    from the fabric side with both port names on it, so the port is only added
    when the service is all we have to go on.
    """
    node = attachment["node"]
    key = (node, client) if node <= client else (client, node)
    link = links.setdefault(key, {"a": key[0], "b": key[1], "ports": {}, "states": set()})
    link["access"] = True
    side = 0 if key[0] == node else 1
    if not any(ports[side] == attachment["port"] for ports in link["ports"]):
        pair = (attachment["port"], "") if side == 0 else ("", attachment["port"])
        link["ports"][pair] = {"a_port": pair[0], "b_port": pair[1]}
    if attachment["state"]:
        link["states"].add(attachment["state"])


def _record_bundle_link(
    links: Dict[Tuple[str, str], Dict[str, Any]],
    segment: str,
    client: str,
    states: Iterable[str],
) -> None:
    """The cable from an ethernet-segment down to the client on it.

    It stands for the bundle as a whole rather than for any one port of it, so
    it carries no port names: those are on the links from the leaves above.
    """
    key = (segment, client) if segment <= client else (client, segment)
    link = links.setdefault(key, {"a": key[0], "b": key[1], "ports": {}, "states": set()})
    link["access"] = True
    link["states"].update(state for state in states if state)


def _link_payload(link: Dict[str, Any], layer_of: Dict[str, int]) -> Dict[str, Any]:
    states = link["states"]
    if any(state in _DOWN_STATES for state in states):
        state = "down"
    elif states and all(state in _UP_STATES for state in states):
        state = "up"
    else:
        state = "unknown"
    ports = [link["ports"][pair] for pair in sorted(link["ports"])]
    return {
        "a": link["a"],
        "b": link["b"],
        "count": len(ports),
        "ports": ports,
        "state": state,
        # Set for the links inside one tier: a DCGW mesh, a pair of spines.
        "intra_layer": layer_of.get(link["a"]) == layer_of.get(link["b"]),
        # Set for a cable to a client rather than to another node of the fabric.
        "access": bool(link.get("access")),
    }


# --------------------------------------------------------------------------- #
# name resolution
# --------------------------------------------------------------------------- #


def _alias_index(nodes: List[NodeFacts]) -> Dict[str, str]:
    """Map every name a node may be known by onto its inventory name.

    LLDP identifies a neighbour by the system-name it advertises, which is
    rarely the name the inventory uses: containerlab prefixes the inventory with
    the lab name while the node keeps its short hostname, and a real fabric
    hands out FQDNs. An alias two nodes could both answer to is dropped rather
    than guessed at.
    """
    direct: Dict[str, Set[str]] = {}
    tails: Dict[str, Set[str]] = {}
    for node in nodes:
        for alias in (node.name, node.hostname, node.system_name):
            if not alias:
                continue
            text = alias.strip().lower()
            short = text.split(".")[0]
            direct.setdefault(text, set()).add(node.name)
            direct.setdefault(short, set()).add(node.name)
            tails.setdefault(_tail(short), set()).add(node.name)
    index = {alias: next(iter(owners)) for alias, owners in direct.items() if len(owners) == 1}
    for alias, owners in tails.items():
        if alias not in index and len(owners) == 1:
            index[alias] = next(iter(owners))
    return index


def _resolve(advertised: str, index: Dict[str, str]) -> Optional[str]:
    """The inventory name of an advertised system-name, if we have that node."""
    text = advertised.strip().lower()
    short = text.split(".")[0]
    return index.get(text) or index.get(short) or index.get(_tail(short))


def _tail(name: str) -> str:
    """The last dash-separated segment: ``clab-dc1-leaf1`` is ``leaf1``."""
    return name.rsplit("-", 1)[-1] or name


# --------------------------------------------------------------------------- #
# state parsing
# --------------------------------------------------------------------------- #


def _adjacencies(system: Dict[str, Any], itf_states: Dict[str, str]) -> List[Adjacency]:
    result = []
    for itf in _as_list(_branch(system, "lldp").get("interface")):
        local = str(itf.get("name", ""))
        if _out_of_band(local):
            continue
        for neighbor in _as_list(itf.get("neighbor")):
            peer = str(neighbor.get("system-name") or "").strip()
            peer_port = str(neighbor.get("port-id") or "")
            if not peer or _out_of_band(peer_port):
                continue
            result.append(
                Adjacency(
                    local_port=local,
                    peer=peer,
                    peer_port=peer_port,
                    oper_state=itf_states.get(local, ""),
                )
            )
    return result


def _out_of_band(port: str) -> bool:
    return port.lower().startswith(_OUT_OF_BAND)


def _segments(system: Dict[str, Any]) -> Dict[str, Segment]:
    """The ethernet-segments of a node, by the port each one is on."""
    evpn = _branch(system, "network-instance", "protocols", "evpn", "ethernet-segments")
    result: Dict[str, Segment] = {}
    for instance in _as_list(evpn.get("bgp-instance")):
        for segment in _as_list(instance.get("ethernet-segment")):
            esi = str(segment.get("esi") or "")
            if not esi:
                continue
            for itf in _as_list(segment.get("interface")):
                port = str(itf.get("ethernet-interface") or "")
                if port:
                    result[port] = Segment(name=str(segment.get("name") or ""), esi=esi)
    return result


def _attachments(
    instance: Dict[str, Any], kind: str, details: Dict[str, Dict[str, Any]]
) -> List[Attachment]:
    """The subinterfaces a service binds, minus the ones no client can be on."""
    service = str(instance.get("name", ""))
    result = []
    for member in _as_list(instance.get("interface")):
        name = str(member.get("name", ""))
        port = name.rsplit(".", 1)[0]
        if not port or port.lower().startswith(_VIRTUAL_PORTS):
            continue
        detail = details.get(name, {})
        result.append(
            Attachment(
                subinterface=name,
                port=port,
                service=service,
                kind=kind,
                vlan=_vlan(detail),
                ip=_first_prefix(detail),
                # The service's own view of the subinterface first: a member of
                # a disabled mac-vrf reads up under /interface and down here.
                oper_state=_norm(member.get("oper-state")) or _norm(detail.get("oper-state")),
            )
        )
    return result


def _subinterface_details(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map ``<port>.<index>`` to what ``/interface`` knows about it.

    The network-instance names the subinterfaces bound to it and nothing more,
    so the vlan and the address a client attaches on come from the other tree.
    """
    details: Dict[str, Dict[str, Any]] = {}
    for itf in _as_list(snapshot.get("interface")):
        if not itf.get("name"):
            continue
        for subinterface in _as_list(itf.get("subinterface")):
            index = subinterface.get("index", subinterface.get("name"))
            if index is None:
                continue
            details[f"{itf['name']}.{index}"] = subinterface
    return details


def _vlan(detail: Dict[str, Any]) -> str:
    """The vlan a subinterface is tagged with, as it would be written down."""
    encap = _branch(detail, "vlan", "encap")
    tagged = encap.get("single-tagged")
    if isinstance(tagged, dict) and tagged.get("vlan-id") not in (None, ""):
        return str(tagged["vlan-id"])
    if "untagged" in encap:
        return "untagged"
    return ""


def _first_prefix(detail: Dict[str, Any]) -> str:
    """The first address of a routed subinterface, v4 before v6."""
    for family in ("ipv4", "ipv6"):
        for address in _as_list(_branch(detail, family).get("address")):
            prefix = address.get("ip-prefix")
            if prefix:
                return str(prefix)
    return ""


def _interface_states(snapshot: Dict[str, Any]) -> Dict[str, str]:
    states = {}
    for itf in _as_list(snapshot.get("interface")):
        if not itf.get("name"):
            continue
        state = _norm(itf.get("oper-state"))
        if state:
            states[str(itf["name"])] = state
    return states


def _is_stitched(instance: Dict[str, Any]) -> bool:
    """Whether a service is stitched: two enabled bgp-vpn instances with RTs.

    The same rule the service reports use to mark a Gateway, so the topology and
    the Bridge Domains and Routers tables cannot disagree on what a DCGW is.
    """
    enabled = 0
    for bgp_instance in _as_list(_branch(instance, "protocols", "bgp-vpn").get("bgp-instance")):
        if _norm(bgp_instance.get("admin-state")) in _DOWN_STATES:
            continue
        if _has_route_target(bgp_instance):
            enabled += 1
    return enabled >= 2


def _has_route_target(bgp_instance: Dict[str, Any]) -> bool:
    config = bgp_instance.get("route-target")
    if not isinstance(config, dict):
        return False
    for key in ("import-rt", "export-rt"):
        raw = config.get(key)
        if isinstance(raw, (str, dict)):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for item in raw:
            target = item.get("target") if isinstance(item, dict) else item
            if target:
                return True
    return False


def _as_list(value: Any) -> List[Dict[str, Any]]:
    """A yang list as a list of its entries.

    gNMI returns a list of one as the entry itself, and a container that is not
    there at all as nothing, so every list has to be read through this.
    """
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _branch(node: Any, *names: str) -> Dict[str, Any]:
    """Descend through nested containers, yielding ``{}`` at the first miss."""
    for name in names:
        if not isinstance(node, dict):
            return {}
        node = node.get(name, {})
    return node if isinstance(node, dict) else {}


def _norm(value: Any) -> str:
    """Normalize a YANG enum leaf to its bare, lower-case value."""
    if not value:
        return ""
    return str(value).lower().split(":")[-1]
