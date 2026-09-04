"""Reading SR Linux ``oper-down-reason`` leaves for what they actually mean.

A down subinterface rarely explains itself. The network-instance holding it says
``subif-down``, the subinterface says ``port-down``, and only its parent port
says something an operator can act on. Reports that stop at the first answer end
up showing a reason that carries no information, so they follow the chain to its
end instead - which is also the only way to see that a port is down because an
ethernet-segment put it in *standby*, and that nothing is wrong at all.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .helpers import as_list, first_payload
from .routing import _gnmi_path_missing, _suppress_pygnmi_client_logging

#: Down reasons that name where to look rather than what is wrong: a
#: network-instance blames its subinterface, a subinterface blames its parent
#: port. Neither is a reason anyone can act on.
DEFERRING_REASONS = frozenset({"subif-down", "port-down"})

#: Down reasons that are configured intent rather than a fault. A port an
#: ethernet-segment put in standby is doing exactly what it was told to, and
#: the subinterfaces on it are down for the same good reason.
INTENT_REASONS = frozenset({"standby-signaling"})

#: What such an interface is reported as instead of a bare ``down``. It says
#: both things that matter: the port really is down, and it is down because it
#: was told to be - so it is neither counted as a fault nor read as forwarding.
STANDBY_STATE = "down/standby"

#: The path the parent-port reasons come from. Narrow on purpose: the whole
#: point is to resolve ``port-down`` without reading every port's counters.
DOWN_REASON_PATH = "/interface[name=*]/oper-down-reason"


def clean_leaf(value: Any) -> str:
    """A YANG enum leaf as a bare lower-case word, without its module prefix."""
    if not value:
        return ""
    return str(value).lower().split(":")[-1]


def parent_interface(subinterface: Any) -> str:
    """The interface a subinterface belongs to: ``lag2.101`` -> ``lag2``."""
    return str(subinterface or "").rsplit(".", 1)[0]


def root_reason(*reasons: Any) -> str:
    """The reason at the end of a chain of ``oper-down-reason`` leaves.

    Pass the chain outermost first: what the network-instance says about its
    member, what the subinterface says about itself, what its parent port says.
    Following the deferrals is what turns three levels of finger-pointing into
    one root cause.
    """
    resolved = ""
    for reason in reasons:
        candidate = clean_leaf(reason)
        if not candidate:
            continue
        if resolved and resolved not in DEFERRING_REASONS:
            # Already at a reason that stands on its own, which a deeper level
            # has no standing to override.
            break
        resolved = candidate
    return resolved


def is_intent(reason: Any) -> bool:
    """Whether a resolved down reason is intent rather than something to fix."""
    return clean_leaf(reason) in INTENT_REASONS


def interface_down_reasons(
    get: Callable[..., List[Dict[str, Any]]],
    interface: str = "*",
) -> Dict[str, str]:
    """Map interface name -> its own ``oper-down-reason``, for the ports that have one.

    SR Linux only carries the leaf while an interface is down, so an all-up node
    answers this with nothing. That is indistinguishable from a node that does
    not know the path, and both mean the same thing here: no parent explains
    anything, so resolving stops at whatever the subinterface said.
    """
    reasons: Dict[str, str] = {}
    path = f"/interface[name={interface}]/oper-down-reason"
    with _suppress_pygnmi_client_logging():
        try:
            resp = get(paths=[path], datatype="state")
        except BaseException as e:
            if _gnmi_path_missing(e):
                return reasons
            raise
    for itf in as_list(first_payload(resp).get("interface")):
        if not isinstance(itf, dict):
            continue
        name = itf.get("name")
        reason = clean_leaf(itf.get("oper-down-reason"))
        if name and reason:
            reasons[str(name)] = reason
    return reasons


class ParentReasons:
    """The down reasons of parent ports, read only if a subinterface needs one.

    A subinterface is the only thing that can ask what is wrong with its port,
    and it only asks by saying ``port-down``. On a fabric where nothing is down
    nothing asks, so the Get behind this is never made - which is why reports
    build one of these per render rather than fetching the reasons up front.
    """

    def __init__(self, get: Callable[..., List[Dict[str, Any]]]) -> None:
        self._get = get
        self._reasons: Optional[Dict[str, str]] = None

    def _lookup(self, subinterface: Any) -> str:
        if self._reasons is None:
            self._reasons = interface_down_reasons(self._get)
        return self._reasons.get(parent_interface(subinterface), "")

    def resolve(self, subinterface: Any, *reasons: Any) -> str:
        """The root cause behind *reasons*, reaching up to the port if they defer to it."""
        reason = root_reason(*reasons)
        if reason not in DEFERRING_REASONS:
            return reason
        return root_reason(reason, self._lookup(subinterface))

    def state(self, state: Any, subinterface: Any, *reasons: Any) -> str:
        """*state*, but ``down/standby`` when it is down only by intent."""
        resolved = clean_leaf(state)
        if resolved == "down" and is_intent(self.resolve(subinterface, *reasons)):
            return STANDBY_STATE
        return resolved
