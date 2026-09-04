"""Fabric sanity checks: the first questions asked of a fabric, answered at once.

A check is a pure function over the payloads report getters already return. It
sees the whole fabric rather than one node, which is what lets it say things a
single table cannot - that a link is only seen from one end, that two leaves
disagree about the VNI of a service.

The payloads, not the rendered tables, are what a check reads. A table exists to
be looked at: its column names carry newlines and sort prefixes, and they are
free to change when the display does. ``spec.getter(device)`` returns the same
structure on every surface, so a check written against it holds on all three.

Adding one means writing a function that takes a :class:`FabricState` and yields
:class:`Finding` objects, then listing it in :data:`CHECKS` with the reports it
reads. Everything else - the CLI command, the MCP tool, the server table - is
driven from that list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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

from .aliases import alias_index, resolve

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

#: Ports that carry management rather than fabric traffic. A management link is
#: not part of the topology and its neighbour is usually not in the inventory.
_OUT_OF_BAND = ("mgmt", "eth0")


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


@dataclass
class FabricState:
    """What the checks see: one report payload per node.

    ``reports[report_name][node]`` is the list a getter returned under its
    resource key, already unwrapped. A node missing from a report is a node the
    report could not be collected from, and :attr:`errors` says why.
    """

    reports: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Inventory node name -> the hostname it is reached on.
    hostnames: Dict[str, str] = field(default_factory=dict)
    #: (report, node) -> why that payload is missing.
    errors: Dict[Tuple[str, str], str] = field(default_factory=dict)

    def nodes(self, report: str) -> List[str]:
        """The nodes *report* was collected from, in inventory order."""
        return list(self.reports.get(report, {}))

    def items(self, report: str) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Every top-level entry of *report*, paired with the node it is from."""
        for node, payload in self.reports.get(report, {}).items():
            for entry in _as_list(payload):
                if isinstance(entry, dict):
                    yield node, entry

    def alias_index(self) -> Dict[str, str]:
        """Resolver from an advertised system-name to an inventory node."""
        names = dict.fromkeys(
            [node for report in self.reports.values() for node in report]
            + list(self.hostnames)
        )
        return alias_index([(node, self.hostnames.get(node, "")) for node in names])


# --------------------------------------------------------------------------- #
# small shared helpers
# --------------------------------------------------------------------------- #


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any) -> str:
    """*value* as the lowercase string a state comparison wants."""
    return str(value if value is not None else "").strip().lower()


def _out_of_band(port: str) -> bool:
    return port.strip().lower().startswith(_OUT_OF_BAND)


def _parent(subinterface: str) -> str:
    """``ethernet-1/1.0`` is a subinterface of ``ethernet-1/1``."""
    return subinterface.rsplit(".", 1)[0]


def _index(subinterface: str) -> str:
    return subinterface.rsplit(".", 1)[-1] if "." in subinterface else ""


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# BGP
# --------------------------------------------------------------------------- #

#: What a neighbour that is not meant to be up reads as. A peer configured and
#: then administratively disabled is not a fault, and the peers report carries
#: no admin-state of its own to tell it apart by.
_BGP_NOT_A_FAULT = {"", "disabled", "-"}

#: The address-family columns of the peers report, and what they are called in
#: a sentence.
_BGP_FAMILIES = {
    "U4\nR/A/T": "ipv4-unicast",
    "U6\nR/A/T": "ipv6-unicast",
    "EVPN\nR/A/T": "evpn",
    "VPNv4\nR/A/T": "l3vpn-ipv4",
    "VPNv6\nR/A/T": "l3vpn-ipv6",
}

#: An address family reads as "received/active/sent" when it is carrying
#: routes, and as a single word when it is not.
_ROUTE_COUNTS = re.compile(r"^(\d+)/(\d+)/(\d+)$")


def _bgp_neighbors(state: FabricState) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """Every BGP neighbour in the fabric, as (node, network-instance, peer)."""
    for node, entry in state.items("bgp_peers"):
        ni = str(entry.get("NI", ""))
        for peer in _as_list(entry.get("Neighbors")):
            if isinstance(peer, dict):
                yield node, ni, peer


def check_bgp_down(state: FabricState) -> List[Finding]:
    """A configured BGP session that is not established."""
    findings = []
    for node, ni, peer in _bgp_neighbors(state):
        session = _text(peer.get("state"))
        if session == "established" or session in _BGP_NOT_A_FAULT:
            continue
        address = peer.get("1_peer", "?")
        group = peer.get("group") or "-"
        findings.append(
            Finding(
                check="bgp_down",
                severity=ERROR,
                node=node,
                subject=f"{ni}/{address}",
                detail=f"session is {session}, peer-group {group}, AS {peer.get('peer-as', '?')}",
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
        if _text(peer.get("state")) != "established":
            continue
        for column, family in _BGP_FAMILIES.items():
            if _text(peer.get(column)) != "down":
                continue
            findings.append(
                Finding(
                    check="bgp_af_down",
                    severity=ERROR,
                    node=node,
                    subject=f"{ni}/{peer.get('1_peer', '?')}",
                    detail=f"session established but {family} is down",
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
        if _text(peer.get("state")) != "established":
            continue
        for column, family in _BGP_FAMILIES.items():
            counts = _ROUTE_COUNTS.match(_text(peer.get(column)))
            if not counts or counts.group(1) != "0":
                continue
            findings.append(
                Finding(
                    check="bgp_no_routes",
                    severity=WARNING,
                    node=node,
                    subject=f"{ni}/{peer.get('1_peer', '?')}",
                    detail=f"{family} is up but has received no routes",
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
# EVPN services
# --------------------------------------------------------------------------- #


def check_evpn_service_mismatch(state: FabricState) -> List[Finding]:
    """Nodes that disagree about a service they both carry.

    A mac-vrf stretched across two leaves has to use the same VNI and the same
    route-targets on both, or the two halves quietly never join up.
    """
    # vxlan-interface -> the VNI it sends on, per node.
    vnis: Dict[Tuple[str, str], Any] = {}
    for node, entry in state.items("vxlan"):
        vnis[(node, str(entry.get("vxlan-itf", "")))] = entry.get("ing-vni")

    # Service name -> {node: what that node thinks the service looks like}.
    services: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for node, entry in state.items("ni"):
        name = str(entry.get("NI", ""))
        kind = _text(entry.get("type"))
        if kind not in ("mac-vrf", "ip-vrf"):
            continue
        interfaces = [
            itf.strip() for itf in str(entry.get("vxlan-itf") or "").split(",") if itf.strip()
        ]
        services.setdefault(name, {})[node] = {
            "vni": ", ".join(str(vnis.get((node, itf), "?")) for itf in interfaces),
            "import-rt": _rt_set(entry.get("In-RT")),
            "export-rt": _rt_set(entry.get("Out-RT")),
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


def _rt_set(value: Any) -> Sequence[str]:
    """Route-targets as a comparable set, however they were written."""
    return sorted(
        {rt.strip().removeprefix("target:") for rt in str(value or "").split(",") if rt.strip()}
    )


def _describe(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value) or "none"
    return str(value) if str(value) else "none"


# --------------------------------------------------------------------------- #
# ethernet segments
# --------------------------------------------------------------------------- #

#: How the ES report writes the designated-forwarder candidates of one
#: network-instance, joined by ", " when a segment is in several:
#: ``macvrf-101:[10.0.0.1 10.0.0.2(DF)], macvrf-202:[10.0.0.1(DF)]``. The comma
#: is excluded from the name so the separator does not read as part of it.
_ES_ASSOCIATION = re.compile(r"(?P<ni>[^:\[\],]+):\[(?P<peers>[^\]]*)\]")


def check_es_df(state: FabricState) -> List[Finding]:
    """Ethernet segments without a working designated-forwarder election.

    A segment with no DF in a network-instance forwards no broadcast traffic
    into it, and two nodes that disagree about the multi-homing mode of one
    segment either duplicate frames or black-hole them.
    """
    findings = []
    modes: Dict[str, Dict[str, str]] = {}

    for node, entry in state.items("es"):
        name = str(entry.get("name", "?"))
        esi = str(entry.get("esi", ""))
        if esi:
            modes.setdefault(esi, {})[node] = _text(entry.get("mh-mode"))

        if _text(entry.get("oper")) not in ("up", ""):
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=name,
                    detail=f"segment is {_text(entry.get('oper'))} on {entry.get('itf/nh') or 'no interface'}",
                )
            )

        for association in _ES_ASSOCIATION.finditer(str(entry.get("ni-peers") or "")):
            peers = association.group("peers").split()
            if any(peer.endswith("(DF)") for peer in peers):
                continue
            findings.append(
                Finding(
                    check="es_df",
                    severity=ERROR,
                    node=node,
                    subject=f"{name}/{association.group('ni').strip()}",
                    detail=(
                        "no designated forwarder elected among "
                        + (" ".join(peers) if peers else "no candidates")
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

    One pass over the fabric per report, each threaded the way a single report
    is. A node that fails one report is still checked against the others. The
    live server does not use this - it has the state already, and builds a
    :class:`FabricState` from its streams instead.
    """
    from nornir.core.task import Result, Task  # noqa: PLC0415 - optional at import

    from .connections.srlinux import CONNECTION_NAME
    from .reports import get_report

    state = FabricState()
    state.hostnames = {
        name: (host.hostname or name) for name, host in target.inventory.hosts.items()
    }
    for report_name in reports:
        spec = get_report(report_name)

        def task_func(task: "Task", spec=spec) -> "Result":
            device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
            return Result(host=task.host, result=spec.getter(device))

        result = target.run(task=task_func, name=spec.resource, raise_on_error=False)
        payloads: Dict[str, Any] = {}
        for node, multi in result.items():
            if multi.failed:
                state.errors[(report_name, node)] = str(multi[0].exception)
                continue
            payloads[node] = (multi[0].result or {}).get(spec.resource) or []
        state.reports[report_name] = payloads
    return state


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
