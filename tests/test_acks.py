"""Acknowledging incidents: out of the counts and colours, until something changes."""

import dataclasses

import pytest
from starlette.testclient import TestClient

from nornir_srl.acks import AckStore, finding_key, is_acknowledged, mark
from nornir_srl.checks import Finding
from nornir_srl.fabric import FabricState
from nornir_srl.incidents import correlate, locate
from nornir_srl.lenses import tree_incidents
from nornir_srl.server.app import create_app
from nornir_srl.server.topology import annotate_health, build_topology

from .test_server_app import fabric  # noqa: F401 - the fixture
from .test_server_timeline import _node

DOWN = Finding("itf_down", "error", "leaf1", "ethernet-1/1.0", "oper down")
BGP = Finding("bgp_down", "error", "leaf1", "default/fe80::2%ethernet-1/1.0", "session is active")
MTU = Finding("mtu_outlier", "warning", "spine1", "ip-mtu", "1500")


def test_an_incident_is_acknowledged_while_every_finding_it_holds_is(tmp_path):
    store = AckStore(tmp_path / "acks.json")
    (incident,) = correlate([DOWN], FabricState())
    store.acknowledge(incident.findings, note="ticket 42", incident=incident.title)
    assert is_acknowledged(incident, store.keys())

    # A second session going down over the same link is news.
    (grown,) = correlate([DOWN, BGP], FabricState())
    assert grown.id == incident.id
    assert not is_acknowledged(grown, store.keys())


def test_acknowledged_incidents_come_last_and_marked():
    incidents = correlate([DOWN, MTU], FabricState())
    acked = {finding_key(DOWN)}
    marked = mark(incidents, acked)
    assert [(i.root.check, i.acknowledged) for i in marked] == [("mtu_outlier", False), ("itf_down", True)]
    card = tree_incidents(marked)[1]
    assert card.action == "unack" and card.state == "" and card.title.startswith("✓")
    assert tree_incidents(marked)[0].action == "ack"


def test_acknowledgements_survive_a_restart_and_can_be_taken_off(tmp_path):
    path = tmp_path / "acks" / "fabric.json"
    AckStore(path).acknowledge([DOWN], note="waiting for optics")
    again = AckStore(path)
    (ack,) = again.all()
    assert (ack.key, ack.note) == (finding_key(DOWN), "waiting for optics")
    assert [a.key for a in again.unacknowledge([finding_key(DOWN)])] == [finding_key(DOWN)]
    assert AckStore(path).keys() == set()


def test_acknowledged_findings_leave_the_badges_and_colours_but_stay_listed():
    graph = build_topology([_node("leaf1", "spine1"), _node("spine1", "leaf1")])
    state = FabricState()
    acked = {finding_key(DOWN)}
    annotate_health(graph, locate([DOWN, MTU], state), mark(correlate([DOWN, MTU], state), acked), acked=acked)
    nodes = {n["name"]: n for n in graph["nodes"]}
    assert nodes["leaf1"]["findings"] == {"error": 0, "warning": 0} and nodes["leaf1"]["health"] == "ok"
    assert [i["acknowledged"] for i in nodes["leaf1"]["issues"]] == [True]
    (link,) = graph["links"]
    assert link["health"] == "ok" and link["findings"][0]["acknowledged"]
    assert graph["summary"][-1].endswith("; 1 acknowledged")


# --------------------------------------------------------------------------- #
# the server
# --------------------------------------------------------------------------- #


def _client(nornir, tmp_path, persist=False):
    return TestClient(
        create_app(
            nornir,
            resync_interval=0,
            restart_debounce=0.02,
            snapshot_dir=tmp_path / "snapshots",
            persist_acks=persist,
        )
    )


def test_acknowledging_over_the_api(fabric, tmp_path):  # noqa: F811 - the fixture
    nornir, devices = fabric
    devices["leaf1"].responses["/network-instance[name=*]/protocols/bgp/neighbor"][0]["network-instance"][0]["protocols"]["bgp"]["neighbor"][0]["session-state"] = "active"
    with _client(nornir, tmp_path, persist=True) as client:
        incidents = client.get("/api/report/incidents").json()["tree"]
        wanted = next(card for card in incidents if card["action"] == "ack")
        before = client.get("/api/overview").json()["health"]

        answer = client.post("/api/ack", json={"incident": wanted["key"], "note": "known"}).json()
        assert answer["acknowledged"] >= 1
        after = client.get("/api/overview").json()["health"]
        assert after["incidents"] == before["incidents"] - 1 and after["acknowledged"] == 1
        card = next(c for c in client.get("/api/report/incidents").json()["tree"] if c["key"] == wanted["key"])
        assert card["action"] == "unack"
        assert client.get("/api/acks").json()["acks"][0]["note"] == "known"
        # ...and it is on the timeline
        changes = client.get("/api/report/changes").json()["rows"]
        assert any(row["Kind"] == "ack" and row["Change"] == "new: acknowledged" for row in changes)

        client.post("/api/unack", json={"incident": wanted["key"]})
        assert client.get("/api/overview").json()["health"]["acknowledged"] == 0
    assert (tmp_path / "acks" / "fabric.json").exists()


def test_acknowledging_an_incident_that_is_not_there(fabric, tmp_path):  # noqa: F811 - the fixture
    nornir, _devices = fabric
    with _client(nornir, tmp_path) as client:
        assert client.post("/api/ack", json={"incident": "link|nowhere"}).status_code == 404
        assert client.post("/api/ack", json={}).status_code == 400


def test_an_acknowledgement_ends_when_its_finding_clears(fabric):  # noqa: F811 - the fixture
    """The same fault coming back later is an alarm again."""
    from nornir_srl.server.store import FabricStore
    from nornir_srl.server.timeline import Watcher

    from .fakes import wait_for
    from .test_server_timeline import BGP_NEIGHBOR, _session_state

    nornir, devices = fabric
    store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02, watch_interval=15)
    store.start()
    try:
        watcher = Watcher(store, store.timeline, interval=0)
        devices["leaf1"].push(BGP_NEIGHBOR, [("session-state", "active")])
        assert wait_for(lambda: _session_state(store, "leaf1") == "active")
        watcher.tick()
        watcher.tick()
        incident = next(i for i in store.timeline.latest.incidents if i.root.check == "bgp_down")
        store.acknowledge(incident.id)
        assert store.acks.keys()

        devices["leaf1"].push(BGP_NEIGHBOR, [("session-state", "established")])
        assert wait_for(lambda: _session_state(store, "leaf1") == "established")
        watcher.tick()
        assert store.acks.keys(), "one reading without it is not yet cleared"
        watcher.tick()
        assert not store.acks.keys()
    finally:
        store.stop()


def test_acknowledgements_last_as_long_as_the_server_unless_asked_to_persist(fabric, tmp_path):  # noqa: F811 - the fixture
    nornir, devices = fabric
    neighbor = devices["leaf1"].responses["/network-instance[name=*]/protocols/bgp/neighbor"][0]
    neighbor["network-instance"][0]["protocols"]["bgp"]["neighbor"][0]["session-state"] = "active"
    for persist in (False, True):
        with _client(nornir, tmp_path, persist=persist) as client:
            card = next(c for c in client.get("/api/report/incidents").json()["tree"] if c["action"] == "ack")
            client.post("/api/ack", json={"incident": card["key"]})
        with _client(nornir, tmp_path, persist=persist) as client:
            acked = client.get("/api/overview").json()["health"]["acknowledged"]
        assert acked == (1 if persist else 0)
        assert (tmp_path / "acks" / "fabric.json").exists() == persist


def test_an_acknowledgement_comes_off_even_once_the_incident_changed_shape(fabric, tmp_path):  # noqa: F811 - the fixture
    """Un-ACK finds the acknowledgements by the id they were made under."""
    from nornir_srl.server.store import FabricStore

    nornir, devices = fabric
    neighbor = devices["leaf1"].responses["/network-instance[name=*]/protocols/bgp/neighbor"][0]
    neighbor["network-instance"][0]["protocols"]["bgp"]["neighbor"][0]["session-state"] = "active"
    store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    store.start()
    try:
        incident = next(i for i in store.health().incidents if i.root.check == "bgp_down")
        store.acknowledge(incident.id)
        # pretend it regrouped: the id no longer names any incident
        store.acks._acks = {
            key: dataclasses.replace(ack, incident_id="gone|" + ack.incident_id) for key, ack in store.acks._acks.items()
        }
        assert store.unacknowledge("gone|" + incident.id)["unacknowledged"] >= 1
        assert not store.acks.keys()
        with pytest.raises(KeyError):
            store.unacknowledge("never|acknowledged")
    finally:
        store.stop()
