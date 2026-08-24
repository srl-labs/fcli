"""Tests for the fcli server store and its HTTP API."""

import json
import os
import tempfile

import pytest
import yaml
from nornir import InitNornir
from starlette.testclient import TestClient

from nornir_srl.server.app import create_app, parse_kv, table_digest, table_events
from nornir_srl.server.reports import REPORTS, REPORTS_BY_NAME, get_report
from nornir_srl.server.rows import flatten, get_fields, is_scalar
from nornir_srl.server.store import FabricStore

from .fakes import (
    LLDP_PATH,
    LLDP_RESPONSE,
    SYS_INFO_RESPONSES,
    FakeDevice,
    wait_for,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


HOSTS = {
    "leaf1": {"hostname": "leaf1", "platform": "srlinux", "data": {"role": "leaf"}},
    "spine1": {"hostname": "spine1", "platform": "srlinux", "data": {"role": "spine"}},
}


def _responses():
    return {LLDP_PATH: LLDP_RESPONSE, **SYS_INFO_RESPONSES}


@pytest.fixture
def fabric(monkeypatch):
    """A two-node Nornir inventory whose connections are FakeDevices."""
    with tempfile.TemporaryDirectory() as tmp:
        host_file = os.path.join(tmp, "hosts.yml")
        group_file = os.path.join(tmp, "groups.yml")
        with open(host_file, "w") as handle:
            yaml.safe_dump(HOSTS, handle)
        with open(group_file, "w") as handle:
            yaml.safe_dump({}, handle)
        nornir = InitNornir(
            inventory={
                "plugin": "SimpleInventory",
                "options": {"host_file": host_file, "group_file": group_file},
            },
            runner={"plugin": "serial"},
            logging={"enabled": False},
        )
        devices = {name: FakeDevice(_responses()) for name in HOSTS}
        monkeypatch.setattr(
            "nornir.core.inventory.Host.get_connection",
            lambda self, name, config: devices[self.name],
        )
        yield nornir, devices


@pytest.fixture
def store(fabric):
    nornir, devices = fabric
    store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    store.start()
    yield store, devices
    store.stop()


@pytest.fixture
def client(fabric):
    nornir, devices = fabric
    app = create_app(nornir, resync_interval=0, refresh=0.5, restart_debounce=0.02)
    with TestClient(app) as test_client:
        yield test_client, devices


# --------------------------------------------------------------------------- #
# row flattening
# --------------------------------------------------------------------------- #


def test_is_scalar():
    assert is_scalar("up")
    assert is_scalar(3)
    assert is_scalar(["a", "b"])
    assert not is_scalar([{"a": 1}])
    assert not is_scalar({"a": 1})


def test_get_fields_sorts_nested_fields():
    item = {"NI": "default", "Rib": [{"b": 1, "a": 2}]}
    assert get_fields(item) == ["NI", "a", "b"]


def test_flatten_expands_nested_lists_into_rows():
    items = [
        {"interface": "ethernet-1/1", "Neighbors": [{"Nbr-System": "spine1"}]},
        {"interface": "ethernet-1/2", "Neighbors": [{"Nbr-System": "spine2"}]},
    ]
    columns, rows = flatten("leaf1", items)
    assert columns == ["interface", "Nbr-System"]
    assert rows == [
        {"Node": "leaf1", "interface": "ethernet-1/1", "Nbr-System": "spine1"},
        {"Node": "leaf1", "interface": "ethernet-1/2", "Nbr-System": "spine2"},
    ]


def test_flatten_handles_flat_items():
    columns, rows = flatten("leaf1", [{"type": "7220 IXR-D2L", "uptime": 10}])
    assert columns == ["type", "uptime"]
    assert rows == [{"Node": "leaf1", "type": "7220 IXR-D2L", "uptime": 10}]


def test_flatten_of_empty_result():
    assert flatten("leaf1", []) == ([], [])


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def test_store_connects_to_every_node(store):
    fabric_store, _devices = store
    inventory = fabric_store.inventory()
    assert {h["name"] for h in inventory} == set(HOSTS)


def test_table_renders_rows_for_all_nodes(store):
    fabric_store, _devices = store
    table = fabric_store.table(get_report("lldp"))
    assert table["columns"] == [
        "Node",
        "interface",
        "Nbr-System",
        "Nbr-port",
        "Nbr-port-desc",
    ]
    assert table["nodes"] == 2
    assert len(table["rows"]) == 4
    assert {row["Node"] for row in table["rows"]} == {"leaf1", "spine1"}
    assert table["errors"] == []


def test_table_subscribes_to_the_paths_the_report_needs(store):
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    assert wait_for(lambda: all(d.subscribe_requests for d in devices.values()))
    for device in devices.values():
        paths = [s["path"] for s in device.subscribe_requests[-1]["subscription"]]
        assert paths == [LLDP_PATH]


def test_paths_are_discovered_once_per_node_and_report(store):
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    after_first = len(devices["leaf1"].subscribe_requests)
    for _ in range(3):
        fabric_store.table(get_report("lldp"))
    assert len(devices["leaf1"].subscribe_requests) == after_first


def test_table_reflects_streamed_updates(store):
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    devices["leaf1"].push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )

    def updated():
        rows = fabric_store.table(get_report("lldp"))["rows"]
        return any(row["Nbr-System"] == "spine9" for row in rows)

    assert wait_for(updated)


def test_inventory_filter_narrows_the_nodes(store):
    fabric_store, _devices = store
    table = fabric_store.table(get_report("lldp"), {"role": "leaf"})
    assert table["nodes"] == 1
    assert {row["Node"] for row in table["rows"]} == {"leaf1"}


def test_report_failure_on_one_node_is_reported_not_raised(store):
    fabric_store, devices = store
    devices["spine1"].responses.pop(LLDP_PATH)
    table = fabric_store.table(get_report("lldp"))
    assert [e["node"] for e in table["errors"]] == ["spine1"]
    assert {row["Node"] for row in table["rows"]} == {"leaf1"}


def test_sys_info_report_uses_multiple_paths(store):
    fabric_store, _devices = store
    table = fabric_store.table(get_report("sys_info"))
    assert len(table["rows"]) == 2
    row = table["rows"][0]
    assert row["type"] == "7220 IXR-D2L"
    assert row["software-version"] == "24.10.1"


def test_status_lists_subscriptions(store):
    fabric_store, _devices = store
    fabric_store.table(get_report("lldp"))
    status = fabric_store.status()
    assert status["subscriptions"] == 2
    assert {node["node"] for node in status["nodes"]} == set(HOSTS)


# --------------------------------------------------------------------------- #
# gNMI session budget
# --------------------------------------------------------------------------- #


def test_repeated_renders_do_not_hit_the_node_again(store):
    """Renders come out of the streamed state, not out of fresh Gets."""
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    settled = len(devices["leaf1"].gets)
    for _ in range(3):
        fabric_store.table(get_report("lldp"))
    assert len(devices["leaf1"].gets) == settled


def test_opening_every_report_costs_one_session_per_node(store):
    """The whole report catalogue shares a single Subscribe RPC per node.

    SR Linux allows 20 concurrent gRPC sessions per server by default, shared
    with every other client of the node, so the path set growing must not grow
    the session count with it.
    """
    fabric_store, devices = store
    for report in REPORTS:
        fabric_store.table(report)
    assert wait_for(
        lambda: fabric_store.status()["max_sessions_per_node"] == 1, timeout=10
    )
    for device in devices.values():
        live = [s for s in device.subscribers if not s.closed]
        assert len(live) == 1


def test_rendering_a_report_keeps_its_paths_subscribed(store):
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    assert wait_for(lambda: devices["leaf1"].subscribe_requests)
    stream = fabric_store._streams["leaf1"]
    stream.idle_timeout = 60.0
    fabric_store.table(get_report("lldp"))
    assert stream._retire_idle_paths() is False
    assert stream.status()["paths"][0]["path"] == LLDP_PATH


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_parse_kv():
    assert parse_kv("role=leaf, site=ams") == {"role": "leaf", "site": "ams"}
    assert parse_kv("") is None
    assert parse_kv(None) is None
    assert parse_kv("nonsense") is None


def test_table_digest_ignores_render_timings():
    base = {"columns": ["Node"], "rows": [{"Node": "leaf1"}], "errors": []}
    assert table_digest({**base, "render_ms": 1}) == table_digest(
        {**base, "render_ms": 99}
    )
    assert table_digest(base) != table_digest({**base, "rows": []})


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


def test_index_is_served(client):
    test_client, _devices = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert "fcli" in response.text


def test_static_assets_are_served(client):
    test_client, _devices = client
    assert test_client.get("/static/app.js").status_code == 200
    assert test_client.get("/static/style.css").status_code == 200


def test_reports_endpoint_lists_every_report(client):
    test_client, _devices = client
    payload = test_client.get("/api/reports").json()
    assert {r["name"] for r in payload["reports"]} == set(REPORTS_BY_NAME)
    assert all(r["title"] and r["description"] for r in payload["reports"])


def test_inventory_endpoint(client):
    test_client, _devices = client
    hosts = test_client.get("/api/inventory").json()["hosts"]
    assert {h["name"] for h in hosts} == set(HOSTS)
    assert {h["labels"]["role"] for h in hosts} == {"leaf", "spine"}
    # the gNMI session is open, but nothing streams until a report is opened
    assert all(h["connected"] and not h["streaming"] for h in hosts)


def test_inventory_reports_streaming_once_a_report_is_open(client):
    test_client, _devices = client
    test_client.get("/api/report/lldp")
    assert wait_for(
        lambda: all(
            h["streaming"] for h in test_client.get("/api/inventory").json()["hosts"]
        )
    )


def test_report_endpoint_returns_a_table(client):
    test_client, _devices = client
    table = test_client.get("/api/report/lldp").json()
    assert table["report"] == "lldp"
    assert len(table["rows"]) == 4


def test_report_endpoint_honours_the_inventory_filter(client):
    test_client, _devices = client
    table = test_client.get("/api/report/lldp?inv_filter=role%3Dspine").json()
    assert {row["Node"] for row in table["rows"]} == {"spine1"}


def test_unknown_report_is_a_404(client):
    test_client, _devices = client
    assert test_client.get("/api/report/nope").status_code == 404
    assert test_client.get("/api/stream/nope").status_code == 404


def test_status_endpoint(client):
    test_client, _devices = client
    test_client.get("/api/report/lldp")
    status = test_client.get("/api/status").json()
    assert len(status["nodes"]) == 2


@pytest.mark.anyio
async def test_stream_pushes_a_table_event(store):
    fabric_store, _devices = store
    events = await _collect(fabric_store, get_report("lldp"), stop_after=1)
    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "table"
    assert payload["report"] == "lldp"
    assert len(payload["rows"]) == 4


@pytest.mark.anyio
async def test_stream_only_pushes_when_the_table_changed(store):
    fabric_store, devices = store
    # Three ticks over unchanged state must produce exactly one table event.
    events = await _collect(fabric_store, get_report("lldp"), stop_after=3)
    assert [kind for kind, _ in events] == ["table"]

    devices["leaf1"].push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )
    assert wait_for(
        lambda: any(
            row["Nbr-System"] == "spine9"
            for row in fabric_store.table(get_report("lldp"))["rows"]
        )
    )
    events = await _collect(fabric_store, get_report("lldp"), stop_after=2)
    assert [kind for kind, _ in events] == ["table"]
    assert any(row["Nbr-System"] == "spine9" for row in events[0][1]["rows"])


@pytest.mark.anyio
async def test_stream_reports_a_render_failure_as_an_error_event(store, monkeypatch):
    fabric_store, _devices = store

    def boom(*_args, **_kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(fabric_store, "table", boom)
    events = await _collect(fabric_store, get_report("lldp"), stop_after=1)
    assert events[0][0] == "error"
    assert events[0][1]["error"] == "render exploded"


async def _collect(fabric_store, report, stop_after):
    """Drive table_events for *stop_after* ticks and parse what it yielded."""
    ticks = {"n": 0}

    async def is_disconnected():
        ticks["n"] += 1
        return ticks["n"] > stop_after

    events = []
    async for chunk in table_events(fabric_store, report, None, 0.01, is_disconnected):
        text = chunk.decode()
        if text.startswith(":"):
            events.append(("keep-alive", None))
            continue
        kind = text.split("\n", 1)[0].removeprefix("event: ")
        data = text.split("data: ", 1)[1].strip()
        events.append((kind, json.loads(data)))
    return events
