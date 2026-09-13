"""One fabric's worth of report payloads, and the small readings of them.

:class:`FabricState` is what anything fabric-wide sees: one report payload per
node, keyed the way a getter returned it. It started inside
:mod:`nornir_srl.checks`, but a check is not the only thing that has to look at
every node at once - a lens (:mod:`nornir_srl.lenses`) answers a question by
joining several reports across the fabric, and reads exactly the same thing. It
lives here so neither module imports the other.

Collecting it is one Nornir pass per report, which is what the CLI and the MCP
surfaces do. The live server never calls :func:`collect_fabric_state`: it has
the state already, and builds a :class:`FabricState` out of its gNMI streams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .aliases import alias_index

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from nornir.core import Nornir

#: Ports that carry management rather than fabric traffic. A management link is
#: not part of the topology and its neighbour is usually not in the inventory.
OUT_OF_BAND = ("mgmt", "eth0")


@dataclass
class FabricState:
    """What a fabric-wide reading sees: one report payload per node.

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
            for entry in as_list(payload):
                if isinstance(entry, dict):
                    yield node, entry

    def sub_items(
        self, report: str, key: str
    ) -> Iterator[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
        """Every nested entry under *key*, with the node and parent it hangs off.

        Most report payloads are two levels deep - a network-instance holding a
        route table, an interface holding its neighbours - and almost every
        reading of one wants both levels at once.
        """
        for node, entry in self.items(report):
            for child in as_list(entry.get(key)):
                if isinstance(child, dict):
                    yield node, entry, child

    def alias_index(self) -> Dict[str, str]:
        """Resolver from an advertised system-name to an inventory node."""
        names = dict.fromkeys(
            [node for report in self.reports.values() for node in report]
            + list(self.hostnames)
        )
        return alias_index([(node, self.hostnames.get(node, "")) for node in names])


# --------------------------------------------------------------------------- #
# small shared readings of a payload
# --------------------------------------------------------------------------- #


def as_list(value: Any) -> List[Any]:
    """*value* as a list, whether it was one, one item, or nothing."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    """*value* as the lowercase string a state comparison wants."""
    return str(value if value is not None else "").strip().lower()


def out_of_band(port: str) -> bool:
    """Whether *port* carries management rather than fabric traffic."""
    return port.strip().lower().startswith(OUT_OF_BAND)


def parent(subinterface: str) -> str:
    """``ethernet-1/1.0`` is a subinterface of ``ethernet-1/1``."""
    return subinterface.rsplit(".", 1)[0]


def index(subinterface: str) -> str:
    """``ethernet-1/1.0`` is index ``0`` of its parent."""
    return subinterface.rsplit(".", 1)[-1] if "." in subinterface else ""


def as_int(value: Any) -> Optional[int]:
    """*value* as an integer, or ``None`` when it is not one."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def collect_fabric_state(
    target: "Nornir", reports: Sequence[str]
) -> FabricState:
    """Run *reports* over a Nornir inventory and return what they answered.

    One pass over the fabric per report, each threaded the way a single report
    is. A node that fails one report is still present in the others, so a
    reading that joins several degrades rather than disappearing.
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
