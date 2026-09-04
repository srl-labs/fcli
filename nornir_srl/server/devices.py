"""Device facades that feed the existing report getters from streamed state.

The getters in :mod:`nornir_srl.connections` only ever touch two things on the
device object: ``self.get(paths=..., datatype=...)`` and ``self.capabilities``.
That makes it possible to run every existing report against a different data
source simply by swapping the ``get`` implementation:

* :class:`RecordingDevice` proxies to the real gNMI connection and records the
  paths a report asks for. That is how the server learns which paths to
  subscribe to, without duplicating the path definitions.
* :class:`CachedDevice` answers from the streamed state tree, falling back to a
  TTL-cached gNMI ``Get`` for paths that could not be subscribed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..connections.down_reason import clean_leaf
from ..connections.ifstats import InterfaceStatsMixin
from ..connections.interfaces import NetworkInstanceMixin
from ..connections.layer2 import Layer2Mixin
from ..connections.neighbor_discovery import NeighborDiscoveryMixin
from ..connections.routing import RoutingMixin
from ..connections.system import SystemMixin
from .stream import HostStream, _suppress_pygnmi_client_logging

logger = logging.getLogger(__name__)


class MixinDevice(
    NetworkInstanceMixin,
    RoutingMixin,
    Layer2Mixin,
    NeighborDiscoveryMixin,
    SystemMixin,
    InterfaceStatsMixin,
):
    """All report getters, with ``get``/``capabilities`` supplied by subclasses."""

    capabilities: Optional[Dict[str, Any]] = None

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class RecordingDevice(MixinDevice):
    """Real device proxy that records every path a report getter requests.

    *getter* redirects the Gets themselves, which is how the server runs
    discovery through a :class:`~nornir_srl.server.stream.HostStream`: they then
    share the one-Get-at-a-time lock and the failure accounting of every other
    Get made against the node, instead of going straight to the connection.
    """

    def __init__(
        self,
        device: Any,
        getter: Optional[Callable[[str, str], List[Dict[str, Any]]]] = None,
    ) -> None:
        self._device = device
        self._getter = getter
        self.capabilities = getattr(device, "capabilities", None)
        self.recorded: List[Tuple[str, str]] = []

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        for path in paths:
            entry = (path, datatype or "config")
            if entry not in self.recorded:
                self.recorded.append(entry)
        if self._getter is None:
            with _suppress_pygnmi_client_logging():
                return self._device.get(
                    paths=paths, datatype=datatype, strip_mod=strip_mod
                )
        result: List[Dict[str, Any]] = []
        for path in paths:
            result.extend(self._getter(path, datatype or "config"))
        return result


class CachedDevice(MixinDevice):
    """Report getters served from a :class:`~nornir_srl.server.stream.HostStream`."""

    def __init__(self, stream: HostStream) -> None:
        self.stream = stream
        self.capabilities = getattr(stream.device, "capabilities", None)

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for path in paths:
            snapshot = self.stream.snapshot(path)
            if snapshot is None:
                snapshot = self.stream.direct_get(path, datatype or "config")
            result.extend(snapshot)
        return result

    def get_ifstats(self, interface: str = "*", interval: int = 5) -> Dict[str, Any]:
        """Interface rates derived from the streamed counter samples.

        Unlike the CLI implementation this does not take two samples of its own:
        the subscription already delivers a fresh sample every interval, and
        :class:`~nornir_srl.server.stream.RateTracker` keeps the derived rates
        up to date.
        """
        rows: List[Dict[str, Any]] = []
        for name in self.stream.interfaces():
            if interface not in ("*", name):
                continue
            itf = self.stream.interface_state(name)
            stats = itf.get("statistics") or {}
            if not stats:
                continue
            rates = self.stream.rates.rates(name)

            def _counter(key: str) -> int:
                try:
                    return int(stats.get(key, 0))
                except (TypeError, ValueError):
                    return 0

            rows.append(
                {
                    "interface": name,
                    "oper-state": itf.get("oper-state", "-"),
                    # Why a port is down is what tells an idle interface that is
                    # meant to be idle - a standby ethernet-segment member -
                    # from one that is not.
                    "down-reason": clean_leaf(itf.get("oper-down-reason")),
                    "in-Kbps": round(rates.get("in-octets", 0.0) * 8 / 1000, 1),
                    "out-Kbps": round(rates.get("out-octets", 0.0) * 8 / 1000, 1),
                    "in-pps": round(rates.get("in-packets", 0.0), 1),
                    "out-pps": round(rates.get("out-packets", 0.0), 1),
                    "in-err": _counter("in-error-packets"),
                    "out-err": _counter("out-error-packets"),
                    "in-disc": _counter("in-discarded-packets"),
                    "out-disc": _counter("out-discarded-packets"),
                    "in-pkts": _counter("in-packets"),
                    "out-pkts": _counter("out-packets"),
                    "in-octets": _counter("in-octets"),
                    "out-octets": _counter("out-octets"),
                }
            )
        return {"ifstats": rows}
