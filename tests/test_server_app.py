"""Tests for the fcli server store and its HTTP API."""

import json
import os
import tempfile
import threading
import time

import pytest
import yaml
from nornir import InitNornir
from starlette.testclient import TestClient

from nornir_srl.reports import SERVER, get_report, reports_for
from nornir_srl.rows import flatten, get_fields, is_scalar
from nornir_srl.server.app import create_app, parse_kv, table_digest, table_events
from nornir_srl.server.store import FabricStore

from .fakes import (
    IFADMIN_PATH,
    IFADMIN_RESPONSE,
    IFSTATE_PATH,
    IFSTATE_RESPONSE,
    IFSTATS_PATH,
    IFSTATS_RESPONSE,
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
    return {
        LLDP_PATH: LLDP_RESPONSE,
        IFSTATS_PATH: IFSTATS_RESPONSE,
        IFSTATE_PATH: IFSTATE_RESPONSE,
        IFADMIN_PATH: IFADMIN_RESPONSE,
        "/network-instance[name=*]/protocols/bgp/neighbor": [
            {
                "network-instance": [
                    {
                        "name": "default",
                        "protocols": {
                            "bgp": {
                                "neighbor": [
                                    {
                                        "peer-address": "192.168.1.1",
                                        "session-state": "established",
                                    }
                                ]
                            }
                        },
                    }
                ]
            }
        ],
        **SYS_INFO_RESPONSES,
    }


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


def test_store_stop_closes_host_connections(fabric):
    nornir, devices = fabric
    fabric_store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    fabric_store.start()
    fabric_store.stop()
    assert fabric_store._stop.is_set()
    fabric_store.stop()  # a second Ctrl+C / lifespan must not raise
    table = fabric_store.table(get_report("lldp"))
    assert table["rows"] == []
    assert table["errors"] == []


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


def test_table_caching_and_invalidation(store):
    """Consecutive calls to table() return cached dict unless state updates."""
    fabric_store, devices = store
    t1 = fabric_store.table(get_report("lldp"))
    t2 = fabric_store.table(get_report("lldp"))
    assert t1 is t2  # Cache hit returns exact same dict object

    # Simulating a state update invalidates the cache
    devices["leaf1"].push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine99")],
    )
    assert wait_for(
        lambda: any(
            row["Nbr-System"] == "spine99"
            for row in fabric_store.table(get_report("lldp"))["rows"]
        )
    )
    t3 = fabric_store.table(get_report("lldp"))
    assert t3 is not t1


def test_all_static_reports_have_predeclared_subscriptions():
    """Verify that core static reports have explicit subscribe specs defined."""
    reports_with_subscribe = [r for r in reports_for(SERVER) if r.subscribe]
    assert len(reports_with_subscribe) >= 10


def test_opening_every_report_costs_one_session_per_node(store):
    """The whole report catalogue shares a single Subscribe RPC per node.

    SR Linux allows 20 concurrent gRPC sessions per server by default, shared
    with every other client of the node, so the path set growing must not grow
    the session count with it.
    """
    fabric_store, devices = store
    for report in reports_for(SERVER):
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
# connecting
# --------------------------------------------------------------------------- #


def test_a_node_that_was_down_at_startup_is_picked_up_later(fabric, monkeypatch):
    """A node still booting when the server starts must not stay unreachable.

    Opening a gNMI connection reaches the node to fetch its certificate, so a
    node that is not up yet fails at startup. Nothing else would retry it: the
    resync sweep only walks nodes that already connected.
    """
    nornir, devices = fabric
    down = {"leaf1"}

    def get_connection(self, name, config):
        if self.name in down:
            raise RuntimeError("The SSL certificate cannot be retrieved")
        return devices[self.name]

    monkeypatch.setattr(
        "nornir.core.inventory.Host.get_connection", get_connection, raising=True
    )
    fabric_store = FabricStore(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        connect_retry_interval=0.0,
    )
    fabric_store.start()
    try:
        table = fabric_store.table(get_report("lldp"))
        assert [e["node"] for e in table["errors"]] == ["leaf1"]
        assert [n["node"] for n in fabric_store.status()["unreachable"]] == ["leaf1"]

        down.clear()  # the node finishes booting
        # The render that schedules the retry still reports the node as down,
        # since reconnecting happens in the background.
        fabric_store.table(get_report("lldp"))
        assert wait_for(lambda: "leaf1" in fabric_store._streams, timeout=10)

        table = fabric_store.table(get_report("lldp"))
        assert table["errors"] == []
        assert fabric_store.status()["unreachable"] == []
        assert {row["Node"] for row in table["rows"]} == {"leaf1", "spine1"}
    finally:
        fabric_store.stop()


def test_a_node_that_stopped_answering_is_not_counted_as_up(store):
    """'connected' has to mean the node answers, not that a stream object exists.

    The gRPC channel behind the stream outlives the node it was opened to, so
    the node pane counted every node as up while the whole fabric was down.
    """
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    assert all(host["connected"] for host in fabric_store.inventory())

    devices["leaf1"].down = True
    fabric_store._streams["leaf1"].resync()

    hosts = {host["name"]: host for host in fabric_store.inventory()}
    assert hosts["leaf1"]["connected"] is False
    assert "GRPC ERROR" in hosts["leaf1"]["error"]
    assert hosts["spine1"]["connected"] is True
    assert hosts["spine1"]["error"] is None


def test_a_node_whose_subscription_dropped_is_not_counted_as_up(store):
    """A dropped Subscribe RPC is the fastest evidence a node went away."""
    fabric_store, devices = store
    fabric_store.table(get_report("lldp"))
    assert wait_for(lambda: all(h["streaming"] for h in fabric_store.inventory()))

    stream = fabric_store._streams["leaf1"]
    stream.connected = False
    stream.error = "GRPC ERROR: Stream removed"

    hosts = {host["name"]: host for host in fabric_store.inventory()}
    assert hosts["leaf1"]["connected"] is False
    assert hosts["spine1"]["connected"] is True


def test_a_node_whose_updates_stopped_arriving_is_not_counted_as_up(store):
    """A subscription can fall silent without the transport reporting anything.

    That is what a vanished route looks like: the connection stays open, gRPC
    keeps considering the call healthy, and only the missing SAMPLE updates say
    the node is gone.
    """
    fabric_store, _devices = store
    fabric_store.table(get_report("lldp"))
    assert wait_for(lambda: all(h["streaming"] for h in fabric_store.inventory()))
    assert all(host["connected"] for host in fabric_store.inventory())

    stream = fabric_store._streams["leaf1"]
    interval = min(s.spec.sample_interval for s in stream._paths.values())
    # Nothing has arrived for well past the interval the node reports on.
    stream.last_update = time.time() - (interval * 3 + 30)
    stream._subscribed_at = stream.last_update

    hosts = {host["name"]: host for host in fabric_store.inventory()}
    assert hosts["leaf1"]["connected"] is False
    assert hosts["spine1"]["connected"] is True


def test_a_node_whose_channel_died_is_given_a_new_connection(fabric, monkeypatch):
    """A redeployed node needs a new connection, not a longer wait.

    Its gRPC channel belongs to the container that went away and keeps failing
    every call from its own state, so the server has to replace it. Waiting was
    what left the whole fabric dead in the UI after a lab restart.
    """
    nornir, devices = fabric
    handed: dict = {}

    def get_connection(self, name, config):
        # The first connection each node gets is one whose calls all fail; any
        # reconnection after that gets a working one.
        if handed.setdefault(self.name, 0) == 0:
            handed[self.name] += 1
            broken = FakeDevice(_responses())
            broken.down = True
            return broken
        return devices[self.name]

    monkeypatch.setattr(
        "nornir.core.inventory.Host.get_connection", get_connection, raising=True
    )
    fabric_store = FabricStore(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        connect_retry_interval=0.0,
    )
    fabric_store.start()
    try:
        table = fabric_store.table(get_report("lldp"))
        assert len(table["errors"]) == 2
        assert table["rows"] == []

        # The render that notices schedules the reconnect; a later one benefits.
        assert wait_for(
            lambda: fabric_store.table(get_report("lldp"))["errors"] == [], timeout=10
        )
        table = fabric_store.table(get_report("lldp"))
        assert {row["Node"] for row in table["rows"]} == {"leaf1", "spine1"}
    finally:
        fabric_store.stop()


def test_a_report_a_node_could_not_serve_is_probed_again(fabric, monkeypatch):
    """Discovery failures must not be a permanent verdict either.

    Discovery runs the report's getter against the device, so it fails while the
    node is down - and the report has to recover once the node is back, without
    the node itself having to be reconnected.
    """
    nornir, devices = fabric
    monkeypatch.setattr(
        "nornir.core.inventory.Host.get_connection",
        lambda self, name, config: devices[self.name],
        raising=True,
    )
    fabric_store = FabricStore(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        connect_retry_interval=0.0,
    )
    fabric_store.start()
    try:
        for device in devices.values():
            device.down = True
        assert len(fabric_store.table(get_report("lldp"))["errors"]) == 2

        for device in devices.values():
            device.down = False
        assert wait_for(
            lambda: fabric_store.table(get_report("lldp"))["errors"] == [], timeout=10
        )
    finally:
        fabric_store.stop()


def test_an_unreachable_node_is_not_retried_on_every_render(fabric, monkeypatch):
    """Retries are rate-limited, so a down node does not slow every render."""
    nornir, _devices = fabric
    attempts = []

    def get_connection(self, name, config):
        attempts.append(self.name)
        raise RuntimeError("unreachable")

    monkeypatch.setattr(
        "nornir.core.inventory.Host.get_connection", get_connection, raising=True
    )
    fabric_store = FabricStore(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        connect_retry_interval=300.0,
    )
    fabric_store.start()
    try:
        settled = len(attempts)
        for _ in range(3):
            fabric_store.table(get_report("lldp"))
        assert len(attempts) == settled
    finally:
        fabric_store.stop()


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
    assert {r["name"] for r in payload["reports"]} == {
        r.name for r in reports_for(SERVER)
    }
    assert all(r["title"] and r["description"] for r in payload["reports"])


def test_reports_endpoint_returns_topo_name(fabric):
    nornir, _devices = fabric
    app = create_app(nornir, resync_interval=0, topo_name="dc1")
    with TestClient(app) as test_client:
        payload = test_client.get("/api/reports").json()
        assert payload["topo_name"] == "dc1"


def test_inventory_endpoint(client):
    test_client, _devices = client
    hosts = test_client.get("/api/inventory").json()["hosts"]
    assert {h["name"] for h in hosts} == set(HOSTS)
    assert {h["labels"]["role"] for h in hosts} == {"leaf", "spine"}
    # the gNMI session is open, but nothing streams until a report is opened
    assert all(h["connected"] and not h["streaming"] and not h["getting"] for h in hosts)


def test_inventory_counts_the_gets_a_node_was_asked_for(store):
    """The Nodes pane watches this counter move: a Get is over in milliseconds.

    Sampling 'a Get is in flight' would practically never catch one, so the
    transfer mark keys off the count having changed between two polls.
    """
    fabric_store, _devices = store
    fabric_store.table(get_report("lldp"))
    before = {h["name"]: h["gets"] for h in fabric_store.inventory()}
    assert all(count > 0 for count in before.values())

    fabric_store._streams["leaf1"].direct_get(IFSTATE_PATH, "state")
    after = {h["name"]: h["gets"] for h in fabric_store.inventory()}
    assert after["leaf1"] > before["leaf1"]
    assert after["spine1"] == before["spine1"]


def test_inventory_marks_a_node_while_a_get_is_in_flight(store):
    """The Nodes pane uses this to show a transfer mark without flipping the dot red."""
    fabric_store, devices = store
    release = threading.Event()
    started = threading.Event()
    orig = devices["leaf1"].get

    def slow(paths, datatype="config", strip_mod=True):
        started.set()
        release.wait(timeout=10)
        return orig(paths, datatype, strip_mod)

    devices["leaf1"].get = slow
    stream = fabric_store._streams["leaf1"]
    worker = threading.Thread(
        target=lambda: stream.direct_get(LLDP_PATH, "state"), daemon=True
    )
    try:
        worker.start()
        assert wait_for(started.is_set)
        hosts = {h["name"]: h for h in fabric_store.inventory()}
        assert hosts["leaf1"]["getting"] is True
        assert hosts["leaf1"]["connected"] is True
        assert hosts["spine1"]["getting"] is False
    finally:
        release.set()
        worker.join(timeout=10)


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


def test_overview_endpoint(client):
    test_client, _devices = client
    resp = test_client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"]["total"] == 2
    assert "connected" in data["nodes"]
    assert "established" in data["bgp"]
    assert "total" in data["interfaces"]
    assert "subscriptions" in data["telemetry"]
    assert "bridge_domains" in data
    assert "routers" in data
    assert "total" in data["bridge_domains"]
    assert "total" in data["routers"]


def test_store_overview_method(store):
    fabric_store, _devices = store
    data = fabric_store.overview()
    assert data["nodes"]["total"] == 2
    assert data["nodes"]["connected"] == 2
    assert isinstance(data["bgp"]["established"], int)
    assert isinstance(data["interfaces"]["total"], int)
    assert isinstance(data["telemetry"]["subscriptions"], int)
    assert isinstance(data["bridge_domains"]["total"], int)
    assert isinstance(data["routers"]["total"], int)


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
async def test_stream_stops_when_the_store_is_stopping(store):
    fabric_store, _devices = store
    fabric_store.stop()

    async def never_disconnects():
        return False

    chunks = [
        chunk
        async for chunk in table_events(
            fabric_store, get_report("lldp"), None, 0.01, never_disconnects
        )
    ]
    assert chunks == []


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


def test_bridge_domains_report_endpoint(client):
    test_client, _devices = client
    resp = test_client.get("/api/report/bridge_domains")
    assert resp.status_code == 200
    table = resp.json()
    assert table["report"] == "bridge_domains"
    assert "columns" in table


def test_overview_ignores_admin_disabled_interfaces(store):
    fabric_store, _devices = store
    # Inject stream tree with admin-disabled interface and admin-enabled oper-down configured interface
    stream = list(fabric_store._streams.values())[0]
    stream._tree["interface"] = [
        {"name": "ethernet-1/1", "admin-state": "enable", "oper-state": "up"},
        {"name": "ethernet-1/2", "admin-state": "disable", "oper-state": "down"},
        {"name": "ethernet-1/3", "admin-state": "enable", "oper-state": "down", "subinterface": [{"index": 0}]},
    ]
    data = fabric_store.overview()
    # ethernet-1/2 (admin disabled) and unconfigured ports are ignored from faults
    # ethernet-1/3 (admin enable, configured, oper down) is counted as down
    assert data["interfaces"]["down"] == 1
    assert data["interfaces"]["total"] == 3

