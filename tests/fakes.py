"""Fake gNMI device used by the fcli server tests.

It mimics the parts of :class:`nornir_srl.connections.srlinux.SrLinux` the
server relies on: ``get``, ``gnmi_subscribe`` and ``capabilities``. Telemetry
notifications are pushed onto a queue in the same shape pygnmi's
``telemetryParser`` produces.
"""

from __future__ import annotations

import queue
import time
from typing import Any, Dict, List, Optional, Tuple

from nornir_srl.connections.subscription import _CLOSED, SubscriptionClosed

CAPABILITIES: Dict[str, Any] = {
    "supported_models": [
        {"name": "urn:srl_nokia/bgp:srl_nokia-bgp", "version": "2023-3-1"},
        {"name": "bgp-rib", "version": "2024-10-31"},
    ]
}


class FakeSubscriber:
    """Stands in for :class:`~nornir_srl.connections.subscription.GnmiSubscription`."""

    def __init__(self, updates: "queue.Queue[Dict[str, Any]]") -> None:
        self._updates = updates
        self.error: Optional[BaseException] = None
        self.closed = False

    def get_update(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        try:
            message = self._updates.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no update")
        if message is _CLOSED:
            raise SubscriptionClosed("subscription was closed")
        return message

    def close(self) -> None:
        self.closed = True
        # Like the real subscription, unblock a reader waiting on the queue
        # rather than letting it sit out its timeout.
        self._updates.put(_CLOSED)


class FakeDevice:
    """A scripted gNMI device."""

    def __init__(
        self,
        responses: Dict[str, List[Dict[str, Any]]],
        *,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.responses = responses
        self.capabilities = capabilities if capabilities is not None else CAPABILITIES
        #: Set to make every RPC fail, as a node that is rebooting does.
        self.down = False
        self.updates: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.gets: List[Tuple[str, Optional[str]]] = []
        self.subscribe_requests: List[Dict[str, Any]] = []
        self.subscribers: List[FakeSubscriber] = []

    # -- SrLinux surface used by the server ---------------------------------

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for path in paths:
            self.gets.append((path, datatype))
            if self.down:
                raise RuntimeError("GRPC ERROR: failed to connect to all addresses")
            if path not in self.responses:
                if path.startswith(("/interface[name=*]", "/network-instance[name=*]")):
                    continue
                raise ValueError(f"unexpected path {path}")
            result.extend(self.responses[path])
        return result

    def gnmi_subscribe(self, subscribe: Dict[str, Any]) -> FakeSubscriber:
        if self.down:
            raise RuntimeError("GRPC ERROR: failed to connect to all addresses")
        self.subscribe_requests.append(subscribe)
        subscriber = FakeSubscriber(self.updates)
        self.subscribers.append(subscriber)
        return subscriber

    # -- test helpers -------------------------------------------------------

    def push(
        self,
        prefix: str,
        updates: Optional[List[Tuple[str, Any]]] = None,
        deletes: Optional[List[str]] = None,
        timestamp: Optional[int] = None,
    ) -> None:
        """Queue one telemetry notification."""
        notification: Dict[str, Any] = {
            "timestamp": timestamp if timestamp is not None else int(time.time() * 1e9),
            "prefix": prefix,
            "update": [{"path": path, "val": value} for path, value in updates or []],
        }
        if deletes:
            notification["delete"] = [{"path": path} for path in deletes]
        self.updates.put({"update": notification})


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll *predicate* until it is true or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------- #
# canned gNMI Get responses
# --------------------------------------------------------------------------- #

LLDP_PATH = "/system/lldp/interface[name=*]/neighbor"
LLDP_RESPONSE: List[Dict[str, Any]] = [
    {
        "system/lldp": {
            "interface": [
                {
                    "name": "ethernet-1/1",
                    "neighbor": [
                        {
                            "id": "1",
                            "port-id": "ethernet-1/49",
                            "system-name": "spine1",
                            "port-description": "to-leaf1",
                        }
                    ],
                },
                {
                    "name": "ethernet-1/2",
                    "neighbor": [
                        {
                            "id": "1",
                            "port-id": "ethernet-1/50",
                            "system-name": "spine2",
                            "port-description": "to-leaf1",
                        }
                    ],
                },
            ]
        }
    }
]

IFSTATS_PATH = "/interface[name=*]/statistics"
IFSTATS_RESPONSE: List[Dict[str, Any]] = [
    {
        "interface": [
            {
                "name": "ethernet-1/1",
                "statistics": {
                    "in-octets": "1000",
                    "out-octets": "2000",
                    "in-packets": "10",
                    "out-packets": "20",
                    "in-error-packets": "0",
                    "out-error-packets": "0",
                    "in-discarded-packets": "0",
                    "out-discarded-packets": "0",
                },
            }
        ]
    }
]

IFSTATE_PATH = "/interface[name=*]/oper-state"
IFSTATE_RESPONSE: List[Dict[str, Any]] = [
    {"interface": [{"name": "ethernet-1/1", "oper-state": "up"}]}
]

IFADMIN_PATH = "/interface[name=*]/admin-state"
IFADMIN_RESPONSE: List[Dict[str, Any]] = [
    {"interface": [{"name": "ethernet-1/1", "admin-state": "enable"}]}
]

#: The LAG report selects a family of interfaces by glob, so it shares the
#: ``interface`` envelope with the wildcard interface subscriptions above.
LAG_PATH = "/interface[name=lag*]"
LAG_RESPONSE: List[Dict[str, Any]] = [
    {
        "interface": [
            {
                "name": "lag1",
                "oper-state": "up",
                "lag": {"lag-type": "lacp", "member": [{"name": "ethernet-1/1"}]},
            }
        ]
    }
]

#: A BGP RIB report reads one branch of ``rib-in-out``, but a subscription on it
#: makes SR Linux stream the sibling branches too, and the getter walks whatever
#: it is handed looking for routes to augment.
RIB_PATH = (
    "/network-instance[name=*]/bgp-rib/afi-safi[afi-safi-name=evpn]/evpn/"
    "rib-in-out/rib-in-post/mac-ip-route"
)
RIB_RESPONSE: List[Dict[str, Any]] = [
    {
        "network-instance": [
            {
                "name": "default",
                "bgp-rib": {
                    "afi-safi": [
                        {
                            "afi-safi-name": "evpn",
                            "evpn": {
                                "rib-in-out": {
                                    "rib-in-post": {
                                        "mac-ip-route": [
                                            {
                                                "path-id": 0,
                                                "attr-id": 1,
                                                "used-route": True,
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    ]
                },
            }
        ]
    }
]

#: A control-plane driven table. SR Linux answers a Get for a subtree that holds
#: nothing with a notification carrying no updates, which normalizes to ``[{}]``.
MAC_PATH = "/network-instance[name=*]/bridge-table/mac-table/mac"
MAC_EMPTY: List[Dict[str, Any]] = [{}]
MAC_RESPONSE: List[Dict[str, Any]] = [
    {
        "network-instance": [
            {
                "name": "vrf-1",
                "bridge-table": {
                    "mac-table": {
                        "mac": [
                            {
                                "address": "00:11:22:33:44:55",
                                "destination": "lag1",
                                "type": "learnt",
                            }
                        ]
                    }
                },
            }
        ]
    }
]

SYS_INFO_RESPONSES: Dict[str, List[Dict[str, Any]]] = {
    "/platform/chassis": [
        {
            "platform/chassis": {
                "type": "7220 IXR-D2L",
                "serial-number": "Sim Serial No.",
                "part-number": "Sim Part No.",
                "hw-mac-address": "1A:F5:00:FF:00:00",
                "last-booted": "2026-08-24T08:00:00.000Z",
            }
        }
    ],
    "/platform/control[slot=A]": [
        {"platform/control[slot=A]": {"software-version": "v24.10.1-492-gabc"}}
    ],
}
