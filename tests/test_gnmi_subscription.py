"""Tests for the Subscribe RPC lifecycle.

The point of :class:`GnmiSubscription` is that closing it cancels the RPC rather
than only half-closing the client side of the stream, because a half-closed
Subscribe leaves the session allocated on the target.
"""

import queue
import threading

import grpc
import pytest

from nornir_srl.connections import subscription as sub_mod
from nornir_srl.connections.subscription import GnmiSubscription

from .fakes import wait_for

REQUEST = {
    "subscription": [{"path": "/interface[name=*]/statistics", "mode": "sample"}],
    "mode": "stream",
    "encoding": "json_ietf",
}


class FakeCall:
    """A server-streaming RPC that only ends when it is cancelled."""

    def __init__(self):
        self.messages: "queue.Queue[object]" = queue.Queue()
        self.cancelled = False
        self.ended = threading.Event()

    def __iter__(self):
        while True:
            if self.cancelled:
                raise grpc.RpcError("cancelled")
            try:
                yield self.messages.get(timeout=0.01)
            except queue.Empty:
                if self.ended.is_set():
                    return
                continue

    def cancel(self):
        self.cancelled = True


class FakeStub:
    def __init__(self, channel):
        self.channel = channel

    def Subscribe(self, request_iterator, metadata=None):  # noqa: N802 - grpc name
        self.channel.requests.append(list(request_iterator))
        self.channel.metadata = metadata
        return self.channel.call


class FakeChannel:
    def __init__(self):
        self.call = FakeCall()
        self.requests = []
        self.metadata = None


class FakeClient:
    """The slice of ``pygnmi.gNMIclient`` a subscription needs."""

    def __init__(self, channel):
        self._gNMIclient__channel = channel
        self._gNMIclient__metadata = [("username", "admin")]
        self.built = []

    def _build_subscriptionrequest(self, subscribe):
        self.built.append(subscribe)
        subscribe["use_aliases"] = False  # pygnmi mutates the dict it is given
        return f"request({len(subscribe['subscription'])} path(s))"


@pytest.fixture
def subscription(monkeypatch):
    monkeypatch.setattr(sub_mod, "gNMIStub", FakeStub)
    monkeypatch.setattr(sub_mod, "telemetryParser", lambda message: message)
    channel = FakeChannel()
    client = FakeClient(channel)
    sub = GnmiSubscription(client, REQUEST, name="leaf1")
    yield sub, channel, client
    sub.close()


def test_close_cancels_the_rpc(subscription):
    sub, channel, _client = subscription
    assert not channel.call.cancelled
    sub.close()
    # A half-close would leave the session allocated on the target; only a
    # cancel releases it.
    assert channel.call.cancelled


def test_close_stops_the_reader_thread(subscription):
    sub, _channel, _client = subscription
    reader = sub._reader
    assert reader.daemon
    sub.close()
    assert wait_for(lambda: not reader.is_alive())


def test_close_does_not_report_the_cancel_as_an_error(subscription):
    sub, _channel, _client = subscription
    sub.close()
    assert sub.error is None


def test_updates_are_parsed_and_returned(subscription):
    sub, channel, _client = subscription
    channel.call.messages.put({"update": {"prefix": "interface"}})
    assert sub.get_update(timeout=2) == {"update": {"prefix": "interface"}}


def test_get_update_times_out_without_traffic(subscription):
    sub, _channel, _client = subscription
    with pytest.raises(TimeoutError):
        sub.get_update(timeout=0.05)


def test_stream_ending_on_its_own_is_an_error(subscription):
    sub, channel, _client = subscription
    channel.call.ended.set()
    # Nothing cancelled the RPC, so the caller has to reconnect rather than
    # keep polling a queue that will never fill again.
    assert wait_for(lambda: sub.error is not None)
    assert "closed by target" in str(sub.error)


def test_the_callers_request_dict_is_not_mutated(subscription):
    _sub, _channel, client = subscription
    assert client.built[0] is not REQUEST
    assert "use_aliases" not in REQUEST


def test_an_unconnected_client_is_rejected(monkeypatch):
    monkeypatch.setattr(sub_mod, "gNMIStub", FakeStub)
    with pytest.raises(ConnectionError, match="no gRPC channel"):
        GnmiSubscription(object(), REQUEST)
