"""Tests for the per-host gNMI subscription session and the device facades."""

import pytest

from nornir_srl.server.devices import CachedDevice, RecordingDevice
from nornir_srl.server.stream import HostStream, RateTracker, SubscriptionSpec

from .fakes import (
    IFSTATE_PATH,
    IFSTATE_RESPONSE,
    IFSTATS_PATH,
    IFSTATS_RESPONSE,
    LLDP_PATH,
    LLDP_RESPONSE,
    FakeDevice,
    wait_for,
)


@pytest.fixture
def lldp_stream():
    device = FakeDevice({LLDP_PATH: LLDP_RESPONSE})
    stream = HostStream("leaf1", device)
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


def test_bootstrap_failure_marks_the_path_unstreamable():
    device = FakeDevice({})
    stream = HostStream("leaf1", device)
    stream.ensure_paths([SubscriptionSpec("/does/not/exist")])
    try:
        assert stream.snapshot("/does/not/exist") is None
        status = stream.status()
        assert status["paths"][0]["streaming"] is False
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
    stream = HostStream("leaf1", device)
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
