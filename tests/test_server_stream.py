"""Tests for the per-host gNMI subscription session and the device facades."""

import threading
import time

import pytest

from nornir_srl.server.devices import CachedDevice, RecordingDevice
from nornir_srl.reports import SubscriptionSpec
from nornir_srl.server.stream import HostStream, RateTracker

from .fakes import (
    IFSTATE_PATH,
    IFSTATE_RESPONSE,
    IFSTATS_PATH,
    IFSTATS_RESPONSE,
    LAG_PATH,
    LAG_RESPONSE,
    MAC_EMPTY,
    MAC_PATH,
    MAC_RESPONSE,
    RIB_PATH,
    RIB_RESPONSE,
    LLDP_PATH,
    LLDP_RESPONSE,
    FakeDevice,
    wait_for,
)

#: The production debounce collapses a page load's worth of report activations
#: into one re-subscription; tests only need it to be non-blocking.
TEST_DEBOUNCE = 0.02


@pytest.fixture
def lldp_stream():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec(LLDP_PATH, "state", sample_interval=20)])
    yield stream, device
    stream.stop()


# --------------------------------------------------------------------------- #
# bootstrap + subscription wiring
# --------------------------------------------------------------------------- #


def test_bootstrap_seeds_the_tree_from_a_get(lldp_stream):
    stream, device = lldp_stream
    assert (LLDP_PATH, "state") in device.gets
    assert stream.snapshot(LLDP_PATH) == LLDP_RESPONSE


def test_subscription_request_uses_sample_mode_in_nanoseconds(lldp_stream):
    stream, device = lldp_stream
    assert wait_for(lambda: device.subscribe_requests)
    request = device.subscribe_requests[0]
    assert request["mode"] == "stream"
    assert request["encoding"] == "json_ietf"
    assert request["subscription"] == [
        {"path": LLDP_PATH, "mode": "sample", "sample_interval": 20_000_000_000}
    ]


def test_unsubscribed_path_is_not_snapshotted(lldp_stream):
    stream, _device = lldp_stream
    assert stream.snapshot("/system/features") is None


def test_a_failed_bootstrap_leaves_the_path_pending_with_its_error():
    """A Get that fails must not be taken as proof the path cannot be streamed.

    The path stays a candidate, so it is retried rather than written off; a node
    that is merely unreachable would otherwise never come back.
    """
    device = FakeDevice({})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec("/does/not/exist")])
    try:
        assert stream.snapshot("/does/not/exist") is None
        status = stream.status()
        assert status["paths"][0]["streaming"] is False
        assert status["paths"][0]["pending"] is True
        assert "unexpected path" in status["paths"][0]["error"]
    finally:
        stream.stop()


def test_status_reports_connection_and_paths(lldp_stream):
    stream, _device = lldp_stream
    assert wait_for(lambda: stream.status()["connected"])
    status = stream.status()
    assert status["node"] == "leaf1"
    assert status["paths"][0]["path"] == LLDP_PATH
    assert status["paths"][0]["streaming"] is True


def test_status_counts_one_session_for_the_subscription(lldp_stream):
    stream, _device = lldp_stream
    assert wait_for(lambda: stream.status()["sessions"] == 1)


# --------------------------------------------------------------------------- #
# keeping the session count down
# --------------------------------------------------------------------------- #


def test_a_burst_of_path_additions_causes_one_resubscribe():
    """Opening several reports must not cost one Subscribe RPC per report."""
    device = FakeDevice(
        {
            LLDP_PATH: LLDP_RESPONSE,
            IFSTATS_PATH: IFSTATS_RESPONSE,
            IFSTATE_PATH: IFSTATE_RESPONSE,
        }
    )
    stream = HostStream("leaf1", device, restart_debounce=0.2)
    try:
        for path in (LLDP_PATH, IFSTATS_PATH, IFSTATE_PATH):
            stream.ensure_paths([SubscriptionSpec(path, "state")])
        assert wait_for(lambda: device.subscribe_requests)
        time.sleep(0.5)
        assert len(device.subscribe_requests) == 1
        subscribed = {s["path"] for s in device.subscribe_requests[0]["subscription"]}
        assert subscribed == {LLDP_PATH, IFSTATS_PATH, IFSTATE_PATH}
    finally:
        stream.stop()


def test_re_asserting_known_paths_does_not_resubscribe(lldp_stream):
    stream, device = lldp_stream
    assert wait_for(lambda: device.subscribe_requests)
    for _ in range(5):
        stream.ensure_paths([SubscriptionSpec(LLDP_PATH, "state", sample_interval=20)])
    time.sleep(0.3)
    assert len(device.subscribe_requests) == 1


def test_idle_paths_are_retired():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream(
        "leaf1", device, restart_debounce=TEST_DEBOUNCE, idle_timeout=0.01
    )
    try:
        stream.ensure_paths([SubscriptionSpec(LLDP_PATH, "state")])
        assert wait_for(lambda: device.subscribe_requests)
        assert wait_for(lambda: stream.status()["paths"] == [], timeout=5)
        assert stream.snapshot(LLDP_PATH) is None
        # the subscription carrying the retired path is torn down as well
        assert wait_for(lambda: device.subscribers[0].closed)
    finally:
        stream.stop()


def test_a_glob_path_does_not_see_what_other_paths_added():
    """The LAG report reads ``interface[name=lag*]``; ifstats reads them all.

    Both land in the same ``interface`` envelope of the shared state tree, so
    the snapshot has to re-apply the glob or the LAG report grows a row per
    ethernet interface.
    """
    device = FakeDevice(
        {
            LAG_PATH: LAG_RESPONSE,
            IFSTATS_PATH: IFSTATS_RESPONSE,
            IFSTATE_PATH: IFSTATE_RESPONSE,
        }
    )
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(LAG_PATH, "all")])
        assert [i["name"] for i in stream.snapshot(LAG_PATH)[0]["interface"]] == [
            "lag1"
        ]

        stream.ensure_paths(
            [SubscriptionSpec(IFSTATS_PATH), SubscriptionSpec(IFSTATE_PATH)]
        )
        assert [i["name"] for i in stream.snapshot(LAG_PATH)[0]["interface"]] == [
            "lag1"
        ]
        # the tree holds both, but each path only sees the entries that carry
        # what it asked for, exactly as its own Get would have reported them
        assert set(stream.interfaces()) == {"lag1", "ethernet-1/1"}
        streamed = {i["name"] for i in stream.snapshot(IFSTATS_PATH)[0]["interface"]}
        assert streamed == {"ethernet-1/1"}
    finally:
        stream.stop()


def test_a_glob_path_ignores_streamed_entries_it_did_not_ask_for():
    device = FakeDevice({LAG_PATH: LAG_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(LAG_PATH, "all")])
        assert wait_for(lambda: device.subscribe_requests)
        device.push("", [("interface[name=ethernet-1/5]/oper-state", "up")])
        assert wait_for(lambda: "ethernet-1/5" in stream.interfaces())
        assert [i["name"] for i in stream.snapshot(LAG_PATH)[0]["interface"]] == [
            "lag1"
        ]
    finally:
        stream.stop()


def test_paths_a_report_still_reads_are_not_retired(lldp_stream):
    stream, _device = lldp_stream
    stream.idle_timeout = 60.0
    assert stream.snapshot(LLDP_PATH) is not None
    assert stream._retire_idle_paths() is False
    assert stream.status()["paths"][0]["path"] == LLDP_PATH


# --------------------------------------------------------------------------- #
# streamed updates
# --------------------------------------------------------------------------- #


def test_streamed_leaf_update_is_visible_in_the_snapshot(lldp_stream):
    stream, device = lldp_stream
    device.push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )
    assert wait_for(
        lambda: stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"][0][
            "neighbor"
        ][0]["system-name"]
        == "spine9"
    )
    # untouched leaves of the same entry survive the merge
    neighbor = stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"][0]["neighbor"][
        0
    ]
    assert neighbor["port-id"] == "ethernet-1/49"


def test_streamed_delete_removes_a_list_entry(lldp_stream):
    stream, device = lldp_stream
    device.push("system/lldp", deletes=["interface[name=ethernet-1/2]"])
    assert wait_for(
        lambda: len(stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"]) == 1
    )


def test_streamed_delete_handles_dict_elem_paths(lldp_stream):
    stream, device = lldp_stream
    # Directly queue a gNMI delete message containing dict items {"path": ...}
    device.updates.put(
        {"update": {"prefix": "system/lldp", "delete": [{"path": "interface[name=ethernet-1/2]"}]}}
    )
    assert wait_for(
        lambda: len(stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"]) == 1
    )


def test_streamed_update_reaches_the_report_getter(lldp_stream):
    stream, device = lldp_stream
    cached = CachedDevice(stream)
    assert (
        cached.get_lldp_sum()["lldp_nbrs"][0]["Neighbors"][0]["Nbr-System"] == "spine1"
    )
    device.push(
        "system/lldp/interface[name=ethernet-1/1]",
        [("neighbor[id=1]/system-name", "spine9")],
    )
    assert wait_for(
        lambda: cached.get_lldp_sum()["lldp_nbrs"][0]["Neighbors"][0]["Nbr-System"]
        == "spine9"
    )


def test_resync_drops_state_the_target_stopped_reporting(lldp_stream):
    stream, device = lldp_stream
    device.push(
        "system/lldp/interface[name=ethernet-1/3]",
        [("neighbor[id=1]/system-name", "ghost")],
    )
    assert wait_for(
        lambda: len(stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"]) == 3
    )
    stream.resync()
    assert len(stream.snapshot(LLDP_PATH)[0]["system/lldp"]["interface"]) == 2


def test_a_report_does_not_see_the_sibling_branches_srlinux_streams():
    """Subscribing to one branch of ``rib-in-out`` gets the others streamed too.

    The BGP RIB getters recurse through their response to attach path attributes
    to every route they find, and only the ``rib-in-post`` routes carry the
    fields they read, so the siblings must not reach them.
    """
    device = FakeDevice({RIB_PATH: RIB_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(RIB_PATH, "state")])
        assert wait_for(lambda: device.subscribe_requests)
        before = stream.status()["last_update"]
        device.push(
            "network-instance[name=default]/bgp-rib/"
            "afi-safi[afi-safi-name=evpn]/evpn/rib-in-out",
            [("rib-in-pre/mac-ip-route[path-id=0]/attr-id", 2)],
        )
        assert wait_for(lambda: stream.status()["last_update"] != before)

        rib_in_out = stream.snapshot(RIB_PATH)[0]["network-instance"][0]["bgp-rib"][
            "afi-safi"
        ][0]["evpn"]["rib-in-out"]
        assert list(rib_in_out) == ["rib-in-post"]
    finally:
        stream.stop()


# --------------------------------------------------------------------------- #
# paths that are empty when the report is opened
# --------------------------------------------------------------------------- #


def test_an_empty_path_is_pending_rather_than_unstreamable():
    """A table with no entries yet says nothing about its envelope shape."""
    device = FakeDevice({MAC_PATH: MAC_EMPTY})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE, get_ttl=0)
    try:
        stream.ensure_paths([SubscriptionSpec(MAC_PATH, "state")])
        path = stream.status()["paths"][0]
        assert path["pending"] is True
        assert path["streaming"] is False
        assert path["error"] is None
        # there is nothing to subscribe to yet, so no RPC is spent on it
        time.sleep(0.2)
        assert device.subscribe_requests == []
        # the report still renders, off a TTL-cached Get
        assert stream.snapshot(MAC_PATH) is None
        assert stream.direct_get(MAC_PATH, "state") == MAC_EMPTY
    finally:
        stream.stop()


def test_the_bootstrap_response_serves_the_first_render():
    device = FakeDevice({MAC_PATH: MAC_EMPTY})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(MAC_PATH, "state")])
        after_bootstrap = len(device.gets)
        CachedDevice(stream).get(paths=[MAC_PATH], datatype="state")
        assert len(device.gets) == after_bootstrap
    finally:
        stream.stop()


def test_a_pending_path_starts_streaming_once_it_has_entries():
    device = FakeDevice({MAC_PATH: MAC_EMPTY})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE, get_ttl=0)
    try:
        stream.ensure_paths([SubscriptionSpec(MAC_PATH, "state")])
        assert stream.status()["paths"][0]["pending"] is True

        # the network learns a MAC; the next render's fallback Get picks it up
        device.responses[MAC_PATH] = MAC_RESPONSE
        cached = CachedDevice(stream)
        assert cached.get(paths=[MAC_PATH], datatype="state") == MAC_RESPONSE

        assert wait_for(lambda: stream.status()["paths"][0]["streaming"])
        assert wait_for(lambda: device.subscribe_requests)
        assert device.subscribe_requests[0]["subscription"][0]["path"] == MAC_PATH
        assert stream.snapshot(MAC_PATH) == MAC_RESPONSE
    finally:
        stream.stop()


def test_a_promoted_path_then_tracks_streamed_updates():
    device = FakeDevice({MAC_PATH: MAC_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec(MAC_PATH, "state")])
        assert wait_for(lambda: device.subscribe_requests)
        device.push(
            "network-instance[name=vrf-1]/bridge-table/mac-table",
            [("mac[address=00:AA:BB:CC:DD:EE]/destination", "lag2")],
        )
        assert wait_for(
            lambda: len(
                stream.snapshot(MAC_PATH)[0]["network-instance"][0]["bridge-table"][
                    "mac-table"
                ]["mac"]
            )
            == 2
        )
    finally:
        stream.stop()


def test_a_response_that_cannot_be_placed_in_the_tree_is_not_streamable():
    device = FakeDevice({"/odd": [{"first": 1, "second": 2}]})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    try:
        stream.ensure_paths([SubscriptionSpec("/odd", "state")])
        path = stream.status()["paths"][0]
        assert path["streaming"] is False
        assert path["pending"] is False
    finally:
        stream.stop()


# --------------------------------------------------------------------------- #
# fallback Gets
# --------------------------------------------------------------------------- #


def test_cached_device_falls_back_to_a_get_for_unsubscribed_paths(lldp_stream):
    stream, device = lldp_stream
    device.responses["/system/features"] = [{"system/features": ["bridged"]}]
    cached = CachedDevice(stream)
    assert cached.get(paths=["/system/features"], datatype="state") == [
        {"system/features": ["bridged"]}
    ]


def test_fallback_gets_are_ttl_cached(lldp_stream):
    stream, device = lldp_stream
    device.responses["/system/features"] = [{"system/features": ["bridged"]}]
    before = len(device.gets)
    for _ in range(5):
        stream.direct_get("/system/features", "state")
    assert len(device.gets) == before + 1


def test_a_failing_fallback_get_is_not_repeated_within_the_ttl(lldp_stream):
    """A down node must not be asked again by every report on every render."""
    stream, device = lldp_stream
    device.down = True
    before = len(device.gets)
    for _ in range(5):
        with pytest.raises(Exception):
            stream.direct_get("/system/features", "state")
    assert len(device.gets) == before + 1


# --------------------------------------------------------------------------- #
# surviving a node restart
# --------------------------------------------------------------------------- #


def test_a_path_recovers_after_the_node_goes_down_and_comes_back(lldp_stream):
    """Restarting the lab must not cost the node its subscription for good.

    A resync that runs while the node is down used to write the path off as
    unstreamable, which dropped it from the subscription and excluded it from
    every later sweep, so the node stayed dead until the server was restarted.
    """
    stream, device = lldp_stream
    assert wait_for(lambda: stream.status()["paths"][0]["streaming"])

    device.down = True
    stream.resync()
    path = stream.status()["paths"][0]
    assert path["streaming"] or path["pending"], "the path was written off"

    device.down = False
    stream.resync()
    assert wait_for(lambda: stream.status()["paths"][0]["streaming"])
    assert stream.snapshot(LLDP_PATH) == LLDP_RESPONSE


def test_a_resync_against_a_down_node_keeps_the_last_known_state(lldp_stream):
    """Half of a failed re-read must not replace a good tree."""
    stream, device = lldp_stream
    before = stream.snapshot(LLDP_PATH)
    assert before == LLDP_RESPONSE

    device.down = True
    stream.resync()
    assert stream.snapshot(LLDP_PATH) == before


def test_a_resync_subscribes_a_pending_path_that_gained_state():
    """The sweep is also what picks up a table that was empty until now."""
    device = FakeDevice({MAC_PATH: MAC_EMPTY})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec(MAC_PATH)])
    try:
        assert stream.status()["paths"][0]["pending"] is True
        device.responses[MAC_PATH] = MAC_RESPONSE
        stream.resync()
        assert wait_for(lambda: stream.status()["paths"][0]["streaming"])
        assert stream.snapshot(MAC_PATH) == MAC_RESPONSE
    finally:
        stream.stop()


def test_a_hanging_get_counts_as_the_node_not_answering():
    """A Get that never returns leaves the node just as unusable as one that fails.

    gNMI calls carry no deadline, so one issued against an address that stopped
    being routed blocks until TCP gives up while holding the Get lock. Reporting
    the node as fine throughout would hide it from the reconnect logic.
    """
    release = threading.Event()

    class Hanging(FakeDevice):
        def get(self, paths, datatype="config", strip_mod=True):
            release.wait(timeout=10)
            return super().get(paths, datatype, strip_mod)

    device = Hanging({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    worker = threading.Thread(
        target=lambda: stream.direct_get(LLDP_PATH, "state"), daemon=True
    )
    try:
        assert stream.failing_since is None
        worker.start()
        assert wait_for(lambda: stream.failing_since is not None)
        release.set()
        worker.join(timeout=10)
        assert wait_for(lambda: stream.failing_since is None)
    finally:
        release.set()
        stream.stop()


def test_a_node_with_nothing_streamable_is_not_reported_as_connected():
    device = FakeDevice({})
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths([SubscriptionSpec("/does/not/exist")])
    try:
        assert wait_for(lambda: stream.status()["connected"] is False)
        assert stream.status()["sessions"] == 0
    finally:
        stream.stop()


# --------------------------------------------------------------------------- #
# path discovery
# --------------------------------------------------------------------------- #


def test_recording_device_captures_the_paths_a_report_needs():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    recorder = RecordingDevice(device)
    result = recorder.get_lldp_sum()
    assert result["lldp_nbrs"][0]["interface"] == "ethernet-1/1"
    assert recorder.recorded == [(LLDP_PATH, "state")]


def test_recording_device_deduplicates_repeated_paths():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    recorder = RecordingDevice(device)
    recorder.get_lldp_sum()
    recorder.get_lldp_sum()
    assert recorder.recorded == [(LLDP_PATH, "state")]


# --------------------------------------------------------------------------- #
# interface rates
# --------------------------------------------------------------------------- #


def test_rate_tracker_computes_a_rate_between_two_samples():
    tracker = RateTracker()
    tracker.observe("e1", {"in-octets": 1000}, 1_000_000_000)
    assert tracker.rates("e1") == {}
    tracker.observe("e1", {"in-octets": 2000}, 3_000_000_000)
    assert tracker.rates("e1")["in-octets"] == pytest.approx(500.0)


def test_rate_tracker_ignores_counter_resets():
    tracker = RateTracker()
    tracker.observe("e1", {"in-octets": 5000}, 1_000_000_000)
    tracker.observe("e1", {"in-octets": 10}, 3_000_000_000)
    assert tracker.rates("e1")["in-octets"] == 0.0


def test_rate_tracker_skips_samples_that_are_too_close_together():
    tracker = RateTracker()
    tracker.observe("e1", {"in-octets": 1000}, 1_000_000_000)
    tracker.observe("e1", {"in-octets": 2000}, 1_100_000_000)
    assert tracker.rates("e1") == {}


def test_ifstats_report_uses_streamed_counter_samples():
    device = FakeDevice(
        {IFSTATS_PATH: IFSTATS_RESPONSE, IFSTATE_PATH: IFSTATE_RESPONSE}
    )
    stream = HostStream("leaf1", device, restart_debounce=TEST_DEBOUNCE)
    stream.ensure_paths(
        [
            SubscriptionSpec(IFSTATS_PATH, sample_interval=1),
            SubscriptionSpec(IFSTATE_PATH, sample_interval=1),
        ]
    )
    try:
        cached = CachedDevice(stream)
        rows = cached.get_ifstats()["ifstats"]
        assert rows[0]["interface"] == "ethernet-1/1"
        assert rows[0]["oper-state"] == "up"
        assert rows[0]["in-Kbps"] == 0.0  # no second sample yet

        base = 10_000_000_000
        device.push(
            "interface[name=ethernet-1/1]",
            [("statistics", dict(IFSTATS_RESPONSE[0]["interface"][0]["statistics"]))],
            timestamp=base,
        )
        device.push(
            "interface[name=ethernet-1/1]",
            [("statistics/in-octets", "126000")],
            timestamp=base + 1_000_000_000,
        )
        assert wait_for(lambda: cached.get_ifstats()["ifstats"][0]["in-Kbps"] > 0)
        row = cached.get_ifstats()["ifstats"][0]
        # 125000 octets in 1s -> 1 000 Kbps
        assert row["in-Kbps"] == pytest.approx(1000.0)
        assert row["in-octets"] == 126000
    finally:
        stream.stop()
