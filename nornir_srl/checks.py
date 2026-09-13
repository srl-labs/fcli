"""Fabric sanity checks: the first questions asked of a fabric, answered at once.

A check is a pure function over the payloads report getters already return. It
sees the whole fabric rather than one node, which is what lets it say things a
single table cannot - that a link is only seen from one end, that two leaves
disagree about the VNI of a service.

The payloads, not the rendered tables, are what a check reads. A table exists to
be looked at: its column names carry newlines and sort prefixes, and they are
free to change when the display does. ``spec.getter(device)`` returns the same
structure on every surface - the records of :mod:`nornir_srl.records` where a
report has them - so a check written against it holds on all three.

Adding one means writing a function that takes a :class:`FabricState` and yields
:class:`Finding` objects, then listing it in :data:`CHECKS` with the reports it
reads. Everything else - the CLI command, the MCP tool, the server table - is
driven from that list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .aliases import resolve
from .fabric import (
    FabricState,
    as_int as _int,
    as_list as _as_list,
    collect_fabric_state as _collect,
    index as _index,
    out_of_band as _out_of_band,
    text as _text,
)

from .records import Neighbor

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from nornir.core import Nornir

#: What the report holding the findings is called in the registry.
CHECKS_REPORT = "checks"
#: Its columns, in the order they read.
CHECKS_COLUMNS = ("Severity", "Check", "Node", "Subject", "Detail")

#: Findings worth waking someone for: the fabric is not doing what it was
#: built to do.
ERROR = "error"
#: Findings worth reading: legitimate in some fabrics, a fault in most.
WARNING = "warning"

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1}


@dataclass(frozen=True)
class Finding:
    """One thing a check found wrong, on one node."""

    check: str
    severity: str
    node: str
    #: What on that node: an interface, a peer, a service.
    subject: str
    detail: str

    def as_row(self) -> Dict[str, Any]:
        return dict(
            zip(
                CHECKS_COLUMNS,
                (self.severity, self.check, self.node, self.subject, self.detail),
            )
        )


@dataclass(frozen=True)
class Check:
    """One question asked of the fabric."""

    name: str
    title: str
    #: What the reports it reads are called in the registry.
    requires: Tuple[str, ...]
    run: Callable[["FabricState"], List[Finding]]


# --------------------------------------------------------------------------- #
# BGP
# --------------------------------------------------------------------------- #

#: What a neighbour that is not meant to be up reads as. A peer configured and
#: then administratively disabled is not a fault, and the peers report carries
#: no admin-state of its own to tell it apart by.
_BGP_NOT_A_FAULT = {"", "disabled", "-"}


def _bgp_neighbors(state: FabricState) -> Iterator[Tuple[str, str, Neighbor]]:
    """Every BGP neighbour in the fabric, as (node, network-instance, peer)."""
    for node, entry, peer in state.sub_items("bgp_peers", "neighbors"):
        yield node, entry.ni, peer


def check_bgp_down(state: FabricState) -> List[Finding]:
    """A configured BGP session that is not established."""
    findings = []
    for node, ni, peer in _bgp_neighbors(state):
        session = _text(peer.state)
        if session == "established" or session in _BGP_NOT_A_FAULT:
            continue
        findings.append(
            Finding(
                check="bgp_down",
                severity=ERROR,
                node=node,
                subject=f"{ni}/{peer.peer or '?'}",
                detail=(
                    f"session is {session}, peer-group {peer.group or '-'}, "
                    f"AS {peer.peer_as if peer.peer_as is not None else '?'}"
                ),
            )
        )
    return findings


def check_bgp_af_down(state: FabricState) -> List[Finding]:
    """An established session carrying an address family that is down.

    The session hides it: the peers report shows ``established`` while the
    family that actually carries the overlay never came up.
    """
    findings = []
    for node, ni, peer in _bgp_neighbors(state):
        if _text(peer.state) != "established":
            continue
        for family in peer.families:
            if not family.enabled or _text(family.oper) != "down":
                continue
            findings.append(
                Finding(
                    check="bgp_af_down",
                    severity=ERROR,
                    node=node,
                    subject=f"{ni}/{peer.peer or '?'}",
                    detail=f"session established but {family.name} is down",
                )
            )
    return findings


def check_bgp_no_routes(state: FabricState) -> List[Finding]:
    """An established session that has received nothing on a family it negotiated.

    Normal on the day a fabric is built and on a peer that has nothing to
    advertise; on an EVPN session in a running fabric it is the symptom of a
    policy dropping everything, or of a peering that came up after the routes
    it should have learned.
    """
    findings = []
    for node, ni, peer in _bgp_neighbors(state):
        if _text(peer.state) != "established":
            continue
        for family in peer.families:
            if not family.enabled or _text(family.oper) == "down" or family.received:
                continue
            findings.append(
                Finding(
                    check="bgp_no_routes",
                    severity=WARNING,
                    node=node,
                    subject=f"{ni}/{peer.peer or '?'}",
                    detail=f"{family.name} is up but has received no routes",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# interfaces
# --------------------------------------------------------------------------- #


def _subinterfaces(state: FabricState) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """Every subinterface in the fabric, as (node, parent interface, subif)."""
    for node, entry in state.items("subif"):
        parent = str(entry.get("Itf", ""))
        if _out_of_band(parent):
            continue
        for subif in _as_list(entry.get("subitfs")):
            if isinstance(subif, dict):
                yield node, parent, subif


def check_itf_down(state: FabricState) -> List[Finding]:
    """A subinterface that is administratively up but operationally down.

    A subinterface held down on purpose - the standby side of a single-active
    ethernet-segment - reads as ``down/standby`` rather than ``down``, and is
    not a fault.
    """
    findings = []
    for node, _parent_itf, subif in _subinterfaces(state):
        if _text(subif.get("oper")) != "down":
            continue
        if _text(subif.get("admin")) not in ("enable", "", "up"):
            continue
        reason = str(subif.get("down-reason") or "no reason reported")
        findings.append(
            Finding(
                check="itf_down",
                severity=ERROR,
                node=node,
                subject=str(subif.get("Subitf", "?")),
                detail=f"admin enabled but oper down: {reason}",
            )
        )
    return findings


def check_itf_errors(state: FabricState) -> List[Finding]:
    """Packets an interface dropped or failed to receive during the sample.

    The counters are the change over the sampling interval, not the totals, so
    a finding means it is happening now rather than that it once did.
    """
    findings = []
    for node, entry in state.items("ifstats"):
        interface = str(entry.get("interface", ""))
        if _out_of_band(interface):
            continue
        errors = (_int(entry.get("in-err")) or 0) + (_int(entry.get("out-err")) or 0)
        discards = (_int(entry.get("in-disc")) or 0) + (_int(entry.get("out-disc")) or 0)
        if errors:
            findings.append(
                Finding(
                    check="itf_errors",
                    severity=ERROR,
                    node=node,
                    subject=interface,
                    detail=(
                        f"{entry.get('in-err', 0)} in / {entry.get('out-err', 0)} out "
                        "error packets during the sample"
                    ),
                )
            )
        if discards:
            findings.append(
                Finding(
                    check="itf_errors",
                    severity=WARNING,
                    node=node,
                    subject=interface,
                    detail=(
                        f"{entry.get('in-disc', 0)} in / {entry.get('out-disc', 0)} out "
                        "discarded packets during the sample"
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# topology
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Link:
    """One end of an adjacency, as the node holding it sees it."""

    node: str
    port: str
    peer: str
    peer_port: str


def _adjacencies(state: FabricState) -> List[_Link]:
    """Every LLDP adjacency whose neighbour is a node we also have."""
    index = state.alias_index()
    links = []
    for node, entry in state.items("lldp"):
        port = str(entry.get("interface", ""))
        if _out_of_band(port):
            continue
        for neighbor in _as_list(entry.get("Neighbors")):
            if not isinstance(neighbor, dict):
                continue
            peer_port = str(neighbor.get("Nbr-port") or "")
            advertised = str(neighbor.get("Nbr-System") or "")
            if not advertised or _out_of_band(peer_port):
                continue
            peer = resolve(advertised, index)
            if peer and peer != node:
                links.append(_Link(node, port, peer, peer_port))
    return links


def check_lldp_one_sided(state: FabricState) -> List[Finding]:
    """A neighbour one node can see and the node on the other end cannot.

    Both ends run LLDP, so a link either end fails to report is a link that is
    not carrying in both directions - a receive fibre, or a port left down.
    """
    seen = {(link.node, link.port, link.peer, link.peer_port) for link in _adjacencies(state)}
    findings = []
    for link in sorted(seen):
        node, port, peer, peer_port = link
        if (peer, peer_port, node, port) in seen:
            continue
        findings.append(
            Finding(
                check="lldp_one_sided",
                severity=WARNING,
                node=node,
                subject=port,
                detail=f"sees {peer} {peer_port}, which does not see it back",
            )
        )
    return findings


def check_mtu_mismatch(state: FabricState) -> List[Finding]:
    """Two ends of one link that do not agree on the MTU.

    What this costs is a fabric that passes every ping and drops everything
    large, which is the failure everyone spends an afternoon on.
    """
    mtus: Dict[Tuple[str, str], Dict[str, int]] = {}
    for node, parent, subif in _subinterfaces(state):
        mtu = _int(subif.get("ip-mtu"))
        if mtu is not None:
            index = _index(str(subif.get("Subitf", "")))
            mtus.setdefault((node, parent), {})[index] = mtu

    findings = []
    compared = set()
    for link in _adjacencies(state):
        # A link is seen from both ends, and is one finding either way.
        ends = tuple(sorted([(link.node, link.port), (link.peer, link.peer_port)]))
        if ends in compared:
            continue
        compared.add(ends)
        near, far = mtus.get(ends[0], {}), mtus.get(ends[1], {})
        for index in sorted(set(near) & set(far)):
            if near[index] == far[index]:
                continue
            findings.append(
                Finding(
                    check="mtu_mismatch",
                    severity=ERROR,
                    node=ends[0][0],
                    subject=f"{ends[0][1]}.{index}",
                    detail=(
                        f"ip-mtu {near[index]}, but {ends[1][0]} "
                        f"{ends[1][1]}.{index} on the other end has {far[index]}"
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# fabric-wide consistency
# --------------------------------------------------------------------------- #


def outliers(values: Mapping[str, Any], floor: int = 3) -> Dict[str, Tuple[Any, Any]]:
    """The entries of *values* that disagree with what most of them say.

    The fault this catches is the one nothing reports as down: every leaf
    configured the same way except one, which works right up until traffic
    takes the path through it. Nothing in a per-node table shows that, because
    each node on its own looks fine - only the fabric read sideways does.

    Returns ``subject -> (its value, the majority value)`` for every subject
    that is in the minority. *floor* is how many subjects have to agree before
    a majority means anything: two nodes differing are two opinions, not an
    outlier and a norm.
    """
    counts: Dict[Any, int] = {}
    for value in values.values():
        counts[value] = counts.get(value, 0) + 1
    if len(values) < floor or len(counts) < 2:
        return {}
    majority, agreeing = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
    # A plurality is not a norm: with 2/2/1 there is nothing to be an outlier
    # from, and saying so would be inventing a convention the fabric has not.
    if agreeing * 2 <= len(values):
        return {}
    return {
        subject: (value, majority)
        for subject, value in values.items()
        if value != majority
    }


def check_mtu_outlier(state: FabricState) -> List[Finding]:
    """A node whose fabric-facing MTU is not the one the rest of the fabric uses.

    ``mtu_mismatch`` compares the two ends of a cable, so it only sees a
    disagreement where LLDP sees a link. This reads the same values down the
    whole fabric instead, which catches the leaf configured at the default MTU
    on a link whose far end has not been brought up yet - before it is carrying
    anything, rather than after.
    """
    # The MTU a node uses on its fabric ports, when it uses just one. A node
    # with a deliberate mix is not making a claim this check can read.
    per_node: Dict[str, int] = {}
    for node, _parent_itf, subif in _subinterfaces(state):
        name = str(subif.get("Subitf", ""))
        mtu = _int(subif.get("ip-mtu"))
        if mtu is None or name.startswith(("irb", "system", "lo")):
            continue
        seen = per_node.setdefault(node, mtu)
        if seen != mtu:
            per_node[node] = -1  # mixed, so not comparable
    comparable = {node: mtu for node, mtu in per_node.items() if mtu > 0}

    return [
        Finding(
            check="mtu_outlier",
            severity=WARNING,
            node=node,
            subject="ip-mtu",
            detail=(
                f"fabric interfaces use ip-mtu {mine}, where {majority} is what "
                f"{len(comparable) - len(outliers(comparable))} of "
                f"{len(comparable)} nodes use"
            ),
        )
        for node, (mine, majority) in sorted(outliers(comparable).items())
    ]


# --------------------------------------------------------------------------- #
# EVPN services
# --------------------------------------------------------------------------- #


def check_evpn_service_mismatch(state: FabricState) -> List[Finding]:
    """Nodes that disagree about a service they both carry.

    A mac-vrf stretched across two leaves has to use the same VNI and the same
    route-targets on both, or the two halves quietly never join up.
    """
    # vxlan-interface -> the VNI it sends on, per node.
    vnis: Dict[Tuple[str, str], Any] = {}
    for node, vxlan in state.items("vxlan"):
        vnis[(node, vxlan.name)] = vxlan.vni

    # Service name -> {node: what that node thinks the service looks like}.
    services: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for node, instance in state.items("ni"):
        if _text(instance.type) not in ("mac-vrf", "ip-vrf"):
            continue
        services.setdefault(instance.name, {})[node] = {
            "vni": ", ".join(
                str(vnis.get((node, overlay), "?")) for overlay in instance.overlays
            ),
            "import-rt": _rt_set(instance.import_rts),
            "export-rt": _rt_set(instance.export_rts),
        }

    findings = []
    for name, by_node in sorted(services.items()):
        if len(by_node) < 2:
            continue
        for attribute, label in (
            ("vni", "VNI"),
            ("import-rt", "import route-target"),
            ("export-rt", "export route-target"),
        ):
            values = {node: facts[attribute] for node, facts in by_node.items()}
            distinct = {_describe(value) for value in values.values()}
            if len(distinct) < 2:
                continue
            for node in sorted(values):
                others = sorted(set(by_node) - {node})
                findings.append(
                    Finding(
                        check="evpn_service_mismatch",
                        severity=ERROR,
                        node=node,
                        subject=name,
                        detail=(
                            f"{label} {_describe(values[node])}, against "
                            + ", ".join(
                                f"{other} {_describe(values[other])}" for other in others
                            )
                        ),
                    )
                )
    return findings


def _rt_set(targets: Sequence[str]) -> Sequence[str]:
    """Route-targets as a comparable set, however they were written."""
    return sorted({rt.strip().removeprefix("target:") for rt in targets if rt.strip()})


def _describe(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value) or "none"
    return str(value) if str(value) else "none"


# --------------------------------------------------------------------------- #
# ethernet segments
# --------------------------------------------------------------------------- #

def check_es_df(state: FabricState) -> List[Finding]:
    """Ethernet segments without a working designated-forwarder election.

    A segment with no DF in a network-instance forwards no broadcast traffic
    into it, and two nodes that disagree about the multi-homing mode of one
    segment either duplicate frames or black-hole them.
    """
    findings = []
    modes: Dict[str, Dict[str, str]] = {}

    for node, segment in state.items("es"):
        if segment.esi:
            modes.setdefault(segment.esi, {})[node] = _text(segment.mh_mode)

        if _text(segment.oper) not in ("up", ""):
            attached = " ".join(segment.interfaces) or " ".join(
                nh.address for nh in segment.next_hops
            )
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=segment.name or "?",
                    detail=f"segment is {_text(segment.oper)} on {attached or 'no interface'}",
                )
            )

        for association in segment.associations:
            if association.designated is not None:
                continue
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=f"{segment.name or '?'}/{association.ni}",
                    detail=(
                        "no designated forwarder elected among "
                        + (
                            " ".join(c.address for c in association.candidates)
                            or "no candidates"
                        )
                    ),
                )
            )

    for esi, by_node in sorted(modes.items()):
        if len({mode for mode in by_node.values() if mode}) < 2:
            continue
        for node in sorted(by_node):
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=esi,
                    detail=(
                        f"multi-homing mode {by_node[node] or 'unset'}, against "
                        + ", ".join(
                            f"{other} {by_node[other] or 'unset'}"
                            for other in sorted(set(by_node) - {node})
                        )
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

CHECKS: Tuple[Check, ...] = (
    Check(
        name="bgp_down",
        title="BGP sessions that are not established",
        requires=("bgp_peers",),
        run=check_bgp_down,
    ),
    Check(
        name="bgp_af_down",
        title="Established sessions with an address family down",
        requires=("bgp_peers",),
        run=check_bgp_af_down,
    ),
    Check(
        name="bgp_no_routes",
        title="Established sessions that have received no routes",
        requires=("bgp_peers",),
        run=check_bgp_no_routes,
    ),
    Check(
        name="itf_down",
        title="Interfaces enabled but not up",
        requires=("subif",),
        run=check_itf_down,
    ),
    Check(
        name="itf_errors",
        title="Interfaces dropping packets",
        requires=("ifstats",),
        run=check_itf_errors,
    ),
    Check(
        name="lldp_one_sided",
        title="Links only one end can see",
        requires=("lldp",),
        run=check_lldp_one_sided,
    ),
    Check(
        name="mtu_mismatch",
        title="Links whose ends disagree about the MTU",
        requires=("lldp", "subif"),
        run=check_mtu_mismatch,
    ),
    Check(
        name="mtu_outlier",
        title="Nodes whose fabric MTU differs from the rest of the fabric",
        requires=("subif",),
        run=check_mtu_outlier,
    ),
    Check(
        name="evpn_service_mismatch",
        title="Services whose nodes disagree about VNI or route-targets",
        requires=("ni", "vxlan"),
        run=check_evpn_service_mismatch,
    ),
    Check(
        name="es_df",
        title="Ethernet segments without a designated forwarder",
        requires=("es",),
        run=check_es_df,
    ),
)

CHECKS_BY_NAME: Mapping[str, Check] = {check.name: check for check in CHECKS}

#: Every report the checks read, which is what a surface has to collect.
REQUIRED_REPORTS: Tuple[str, ...] = tuple(
    dict.fromkeys(report for check in CHECKS for report in check.requires)
)


def collect_fabric_state(
    target: "Nornir", reports: Sequence[str] = REQUIRED_REPORTS
) -> FabricState:
    """Run the reports the checks read over a Nornir inventory.

    A thin default over :func:`nornir_srl.fabric.collect_fabric_state`: the
    checks read a fixed set of reports, so the caller does not have to name
    them. A lens names its own.
    """
    return _collect(target, reports)


def run_checks(
    state: FabricState, only: Optional[Sequence[str]] = None
) -> List[Finding]:
    """Every finding in the fabric, worst first.

    A check whose reports could not be collected from any node is skipped
    rather than reported clean, and one that raises is reported as a finding of
    its own: a check going wrong is not the same as a fabric being right.
    """
    selected = [c for c in CHECKS if not only or c.name in only]
    findings: List[Finding] = []
    for check in selected:
        if not any(state.reports.get(report) for report in check.requires):
            continue
        try:
            findings.extend(check.run(state))
        except Exception as exc:  # noqa: BLE001 - one bad check is not the fabric
            findings.append(
                Finding(
                    check=check.name,
                    severity=ERROR,
                    node="-",
                    subject="check failed",
                    detail=str(exc),
                )
            )

    for (report, node), error in sorted(state.errors.items()):
        findings.append(
            Finding(
                check="collection",
                severity=WARNING,
                node=node,
                subject=report,
                detail=f"not checked: {error}",
            )
        )

    findings.sort(
        key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.check, f.node, f.subject)
    )
    return findings
