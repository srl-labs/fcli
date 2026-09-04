"""Reading a chain of ``oper-down-reason`` leaves down to its root cause."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from nornir_srl.connections.down_reason import (
    ParentReasons,
    interface_down_reasons,
    is_intent,
    root_reason,
)


class _Device:
    """Answers the one path :func:`interface_down_reasons` reads."""

    def __init__(self, response: Optional[List[Dict[str, Any]]] = None) -> None:
        self.response = response if response is not None else []
        self.requested: List[str] = []

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        self.requested.extend(paths)
        return self.response


def test_root_reason_follows_the_deferrals_to_the_port():
    """A network-instance blames the member, the member blames the port."""
    assert (
        root_reason("subif-down", "port-down", "standby-signaling")
        == "standby-signaling"
    )


def test_root_reason_stops_at_a_reason_that_stands_on_its_own():
    """``admin-down`` explains itself, so nothing below it gets a say."""
    assert root_reason("admin-down", "port-down", "standby-signaling") == "admin-down"


def test_root_reason_skips_levels_that_say_nothing():
    assert root_reason(None, "port-down", "port-admin-disabled") == "port-admin-disabled"
    assert root_reason("net-inst-down", None, None) == "net-inst-down"
    assert root_reason(None, None, None) == ""


def test_root_reason_ignores_the_yang_module_prefix():
    assert root_reason("srl_nokia-interfaces:standby-signaling") == "standby-signaling"


def test_only_standby_signaling_is_intent():
    assert is_intent("standby-signaling")
    assert not is_intent("port-down")
    assert not is_intent("")


def test_interface_down_reasons_keeps_only_the_ports_that_have_one():
    device = _Device(
        [
            {
                "interface": [
                    {"name": "lag2", "oper-down-reason": "standby-signaling"},
                    {"name": "lag1"},
                ]
            }
        ]
    )

    assert interface_down_reasons(device.get) == {"lag2": "standby-signaling"}


def test_interface_down_reasons_survives_a_node_with_nothing_down():
    """SR Linux carries the leaf only while an interface is down."""
    assert interface_down_reasons(_Device().get) == {}


def test_parent_reasons_are_read_once_and_only_when_needed():
    """The Get is the cost, so a reason that explains itself must not spend one."""
    device = _Device(
        [{"interface": [{"name": "lag2", "oper-down-reason": "standby-signaling"}]}]
    )
    parents = ParentReasons(device.get)

    assert parents.resolve("irb0.101", "net-inst-down") == "net-inst-down"
    assert device.requested == []

    assert parents.resolve("lag2.101", "port-down") == "standby-signaling"
    assert parents.resolve("lag2.102", "subif-down", "port-down") == (
        "standby-signaling"
    )
    assert device.requested == ["/interface[name=*]/oper-down-reason"]


def test_parent_reasons_turn_an_intentional_down_into_standby():
    device = _Device(
        [{"interface": [{"name": "lag2", "oper-down-reason": "standby-signaling"}]}]
    )
    parents = ParentReasons(device.get)

    # Both halves of the truth: the port is down, and standby is why.
    assert parents.state("down", "lag2.101", "port-down") == "down/standby"
    # An up subinterface is up whatever its port once said.
    assert parents.state("up", "lag2.101", None) == "up"


def test_parent_reasons_leave_a_real_fault_down():
    device = _Device(
        [{"interface": [{"name": "lag2", "oper-down-reason": "min-links-not-met"}]}]
    )
    parents = ParentReasons(device.get)

    assert parents.state("down", "lag2.101", "port-down") == "down"


def test_parent_reasons_resolve_no_further_when_the_port_says_nothing():
    """A node with nothing down leaves the chain at what the member said."""
    parents = ParentReasons(_Device().get)

    assert parents.resolve("lag2.101", "port-down") == "port-down"
    assert parents.state("down", "lag2.101", "port-down") == "down"
