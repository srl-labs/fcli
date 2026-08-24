"""Per-host gNMI subscription session feeding an in-memory state tree.

Each :class:`HostStream` owns one gNMI ``Subscribe`` RPC per host. The paths it
subscribes to are the very same paths the report getters in
:mod:`nornir_srl.connections` ask for, so the streamed state can be handed back
to those getters unchanged (see :class:`~nornir_srl.server.devices.CachedDevice`).

A path is bootstrapped with a regular gNMI ``Get`` before the subscription is
started: that gives both a complete starting state and the exact response
envelope key SR Linux uses for the path, which is what a ``Get`` caller expects
to find in the returned structure.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..connections.helpers import strip_modules
from .tree import (
    delete,
    get_node,
    insert,
    join_path,
    materialize,
    parse_path,
    select_path,
)

logger = logging.getLogger(__name__)

# Counters used to derive interface rates from consecutive streamed samples.
IFSTATS_COUNTERS: Tuple[str, ...] = (
    "in-octets",
    "out-octets",
    "in-packets",
    "out-packets",
    "in-error-packets",
    "out-error-packets",
    "in-discarded-packets",
    "out-discarded-packets",
)

_MIN_RATE_INTERVAL = 0.5  # seconds; below this the rate is too noisy to report


@dataclass(frozen=True)
class SubscriptionSpec:
    """One gNMI subscription entry."""

    path: str
    datatype: str = "state"
    mode: str = "sample"  # sample | on_change | target_defined
    sample_interval: int = 10  # seconds

    def as_gnmi(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"path": self.path, "mode": self.mode}
        if self.mode == "sample":
            entry["sample_interval"] = int(self.sample_interval * 1_000_000_000)
        return entry


@dataclass
class PathState:
    """Bookkeeping for one subscribed path."""

    spec: SubscriptionSpec
    envelopes: List[str] = field(default_factory=list)
    bootstrapped: bool = False
    error: Optional[str] = None
    streamable: bool = True
    #: When a report last read this path, used to retire unwatched paths.
    last_read: float = field(default_factory=time.time)


class RateTracker:
    """Derives per-interface rates from consecutive streamed counter samples."""

    def __init__(self) -> None:
        self._last: Dict[str, Tuple[float, Dict[str, int]]] = {}
        self._rates: Dict[str, Dict[str, float]] = {}

    def observe(self, itf: str, counters: Dict[str, Any], ts_ns: int) -> None:
        now = ts_ns / 1e9 if ts_ns else time.time()
        current: Dict[str, int] = {}
        for name in IFSTATS_COUNTERS:
            try:
                current[name] = int(counters.get(name, 0))
            except (TypeError, ValueError):
                current[name] = 0
        previous = self._last.get(itf)
        if previous is not None:
            prev_ts, prev_counters = previous
            dt = now - prev_ts
            if dt >= _MIN_RATE_INTERVAL:
                rates = {}
                for name in IFSTATS_COUNTERS:
                    delta = current[name] - prev_counters.get(name, 0)
                    if delta < 0:  # counter reset/wrap
                        delta = 0
                    rates[name] = delta / dt
                self._rates[itf] = rates
                self._last[itf] = (now, current)
            return
        self._last[itf] = (now, current)

    def rates(self, itf: str) -> Dict[str, float]:
        return self._rates.get(itf, {})


class HostStream:
    """Streaming state for a single SR Linux node.

    Every node is served by exactly one ``Subscribe`` RPC carrying the union of
    the paths the opened reports need. gNMI cannot add paths to a running
    subscription, so growing that set means replacing the RPC; restarts are
    therefore coalesced by a background reconciler instead of being done once
    per report, and paths nobody reads any more are retired again. That keeps
    the number of gRPC sessions this node spends near the one-per-node floor,
    well inside SR Linux's default ``session-limit`` of 20.
    """

    def __init__(
        self,
        name: str,
        device: Any,
        *,
        default_sample_interval: int = 10,
        get_ttl: float = 30.0,
        reconnect_delay: float = 5.0,
        restart_debounce: float = 1.0,
        idle_timeout: float = 900.0,
    ) -> None:
        self.name = name
        self.device = device
        self.default_sample_interval = default_sample_interval
        self.get_ttl = get_ttl
        self.reconnect_delay = reconnect_delay
        self.restart_debounce = restart_debounce
        self.idle_timeout = idle_timeout

        self._lock = threading.RLock()
        self._get_lock = threading.Lock()
        self._tree: Dict[str, Any] = {}
        self._paths: Dict[str, PathState] = {}
        self._direct_cache: Dict[
            Tuple[str, str], Tuple[float, List[Dict[str, Any]]]
        ] = {}
        self.rates = RateTracker()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._generation = 0
        self._subscription: Any = None
        self._gets = 0
        self.last_update: Optional[float] = None
        self.connected = False
        self.error: Optional[str] = None

        self._dirty = threading.Event()
        self._reconciler = threading.Thread(
            target=self._reconcile,
            name=f"gnmi-reconcile-{self.name}",
            daemon=True,
        )
        self._reconciler.start()

    # ------------------------------------------------------------------ #
    # subscription lifecycle
    # ------------------------------------------------------------------ #

    def ensure_paths(self, specs: List[SubscriptionSpec]) -> None:
        """Make sure every spec in *specs* is subscribed.

        Newly added paths are bootstrapped with a ``Get`` right away so the
        report that asked for them can be rendered immediately, but replacing
        the ``Subscribe`` RPC is left to the reconciler, which batches the
        restarts caused by opening several reports in a row.
        """
        added = []
        now = time.time()
        with self._lock:
            for spec in specs:
                state = self._paths.get(spec.path)
                if state is not None:
                    state.last_read = now
                    continue
                self._paths[spec.path] = PathState(spec=spec, last_read=now)
                added.append(spec)
        if not added:
            return
        for spec in added:
            self._bootstrap(spec, self._tree)
        self._dirty.set()

    def _bootstrap(self, spec: SubscriptionSpec, tree: Dict[str, Any]) -> None:
        """Seed *tree* with a gNMI Get and learn the response envelope keys."""
        state = self._paths.get(spec.path)
        if state is None:  # retired while we were getting to it
            return
        try:
            resp = self._raw_get(spec.path, spec.datatype)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            state.error = str(exc)
            state.streamable = False
            logger.warning(
                "%s: bootstrap Get failed for %s: %s", self.name, spec.path, exc
            )
            return
        # Serve the first render from this response instead of repeating the Get
        # while the path is still pending.
        self._direct_cache[(spec.path, spec.datatype)] = (time.time(), resp)
        self._absorb(spec, resp, tree)

    def _absorb(
        self, spec: SubscriptionSpec, resp: List[Dict[str, Any]], tree: Dict[str, Any]
    ) -> bool:
        """Learn the envelope keys of *resp* and seed *tree* with its state.

        Returns whether the path is now bootstrapped, i.e. ready to be streamed.

        SR Linux answers a ``Get`` for a subtree that holds nothing with a
        notification carrying no updates, which says nothing about the envelope
        key a ``Get`` caller expects to find. Such a path stays *pending*: it is
        left out of the subscription and served by a TTL-cached ``Get`` until its
        first entry appears. Control-plane driven tables (MAC, ES destinations)
        routinely start out that way.
        """
        state = self._paths.get(spec.path)
        if state is None:
            return False
        envelopes: List[str] = []
        streamable = True
        hints = _key_hints(spec.path)
        for item in resp:
            if not isinstance(item, dict):
                streamable = False
                break
            if not item:  # empty subtree: nothing to learn from yet
                continue
            if len(item) != 1:
                # Keyless yang-list responses cannot be placed in the tree;
                # such a path is served by a TTL-cached Get instead.
                streamable = False
                break
            env_key = next(iter(item))
            env_path = "" if env_key in ("/", "") else env_key
            insert(tree, env_path, item[env_key], key_hints=hints)
            if env_path not in envelopes:
                envelopes.append(env_path)
        with self._lock:
            state.streamable = streamable
            if not streamable:
                state.envelopes = []
                state.bootstrapped = False
                return False
            state.error = None
            if envelopes:
                state.envelopes = envelopes
                state.bootstrapped = True
            return state.bootstrapped

    def resync(self) -> None:
        """Rebuild the whole tree from gNMI Gets, dropping any stale state.

        SAMPLE subscriptions refresh values but rely on the target sending
        deletes for entries that disappear. A periodic full re-read keeps the
        view self-healing if one is ever missed.
        """
        with self._lock:
            specs = [
                state.spec
                for state in self._paths.values()
                if state.streamable and state.bootstrapped
            ]
        if not specs:
            return
        fresh: Dict[str, Any] = {}
        for spec in specs:
            self._bootstrap(spec, fresh)
        with self._lock:
            self._tree = fresh
        self.last_update = time.time()

    def _reconcile(self) -> None:
        """Apply pending path-set changes, one restart per burst.

        Opening a report adds its paths and flags the set dirty. Waiting for the
        flag to stay clear for ``restart_debounce`` before re-subscribing turns
        the burst of activations that a page load produces into a single new
        ``Subscribe`` RPC, instead of one per report.
        """
        while not self._closed.is_set():
            if not self._dirty.wait(timeout=1.0):
                if self._retire_idle_paths():
                    self._restart()
                continue
            while self._dirty.is_set():
                self._dirty.clear()
                if self._closed.wait(self.restart_debounce):
                    return
            self._restart()

    def _retire_idle_paths(self) -> bool:
        """Drop paths no report has read for ``idle_timeout``.

        The streamed values stay behind in the tree, but they are unreachable
        without a :class:`PathState` and get overwritten by a fresh bootstrap if
        the path is ever asked for again. Returns whether anything was dropped.
        """
        if self.idle_timeout <= 0:
            return False
        cutoff = time.time() - self.idle_timeout
        with self._lock:
            idle = [
                path for path, state in self._paths.items() if state.last_read < cutoff
            ]
            for path in idle:
                del self._paths[path]
        if idle:
            logger.info("%s: retired %d idle path(s)", self.name, len(idle))
        return bool(idle)

    def _restart(self) -> None:
        """(Re)start the subscription thread with the current path set."""
        if self._closed.is_set():
            return
        with self._lock:
            self._generation += 1
            generation = self._generation
            specs = [
                s.spec for s in self._paths.values() if s.streamable and s.bootstrapped
            ]
        self._close_subscription()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if not specs or self._closed.is_set():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(generation, specs),
            name=f"gnmi-sub-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def _close_subscription(self) -> None:
        subscription, self._subscription = self._subscription, None
        if subscription is None:
            return
        try:
            subscription.close()
        except Exception as exc:  # noqa: BLE001 - best effort teardown
            logger.debug("%s: closing subscription failed: %s", self.name, exc)

    def _run(self, generation: int, specs: List[SubscriptionSpec]) -> None:
        request = {
            "subscription": [s.as_gnmi() for s in specs],
            "mode": "stream",
            "encoding": "json_ietf",
        }
        while self._alive(generation):
            try:
                subscription = self.device.gnmi_subscribe(request)
                self._subscription = subscription
                self.connected = True
                self.error = None
                logger.info("%s: subscribed to %d path(s)", self.name, len(specs))
                while self._alive(generation):
                    try:
                        message = subscription.get_update(timeout=1.0)
                    except TimeoutError:
                        if subscription.error is not None:
                            raise subscription.error
                        continue
                    if message:
                        self._apply(message)
            except Exception as exc:  # noqa: BLE001 - retried with backoff
                self.connected = False
                self.error = str(exc)
                logger.warning("%s: subscription failed: %s", self.name, exc)
            finally:
                self._close_subscription()
            if not self._alive(generation):
                break
            self._stop.wait(self.reconnect_delay)
        self.connected = False

    def _alive(self, generation: int) -> bool:
        """Whether the subscription of *generation* should still be running.

        ``_stop`` is cleared again by every restart, so a subscription that
        raced with :meth:`stop` also has to check ``_closed`` - otherwise it
        would reconnect and hold a session on the target until the process ends.
        """
        return (
            not self._stop.is_set()
            and not self._closed.is_set()
            and generation == self._generation
        )

    def stop(self) -> None:
        self._closed.set()
        self._stop.set()
        with self._lock:
            self._generation += 1
        self._close_subscription()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if self._reconciler.is_alive():
            self._reconciler.join(timeout=3)

    # ------------------------------------------------------------------ #
    # update handling
    # ------------------------------------------------------------------ #

    def _apply(self, message: Dict[str, Any]) -> None:
        update = message.get("update")
        if not update:
            return
        prefix = update.get("prefix") or ""
        timestamp = update.get("timestamp") or 0
        touched_itfs = set()
        with self._lock:
            for item in update.get("update", []) or []:
                path = join_path(prefix, item.get("path"))
                insert(self._tree, path, item.get("val"))
                itf = _touched_interface(path)
                if itf:
                    touched_itfs.add(itf)
            for item in update.get("delete", []) or []:
                delete(self._tree, join_path(prefix, item.get("path")))
            for itf in touched_itfs:
                stats = get_node(self._tree, f"interface[name={itf}]/statistics")
                if isinstance(stats, dict):
                    self.rates.observe(itf, materialize(stats), timestamp)
        self.last_update = time.time()

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def snapshot(self, path: str) -> Optional[List[Dict[str, Any]]]:
        """Return the streamed state for *path* shaped like a gNMI Get response."""
        with self._lock:
            state = self._paths.get(path)
            if state is None or not state.bootstrapped or not state.streamable:
                return None
            state.last_read = time.time()
            result: List[Dict[str, Any]] = []
            for env in state.envelopes:
                node = self._tree if env == "" else get_node(self._tree, env)
                key = env if env else "/"
                if node is None:
                    result.append({key: {}})
                    continue
                # The tree is shared by every subscription of this node, so the
                # envelope can hold entries this path never asked for.
                result.append({key: select_path(materialize(node), path, env)})
            return result

    def interfaces(self) -> List[str]:
        """Names of the interfaces currently present in the streamed state."""
        with self._lock:
            node = get_node(self._tree, "interface")
            entries = materialize(node) if node is not None else []
        if not isinstance(entries, list):
            return []
        return sorted(str(e.get("name", "")) for e in entries if isinstance(e, dict))

    def interface_state(self, name: str) -> Dict[str, Any]:
        with self._lock:
            node = get_node(self._tree, f"interface[name={name}]")
            return materialize(node) if isinstance(node, dict) else {}

    def direct_get(self, path: str, datatype: str) -> List[Dict[str, Any]]:
        """gNMI Get with a short TTL cache, for paths that are not subscribed."""
        cache_key = (path, datatype)
        now = time.time()
        cached = self._direct_cache.get(cache_key)
        if cached and now - cached[0] < self.get_ttl:
            return cached[1]
        resp = self._raw_get(path, datatype)
        self._direct_cache[cache_key] = (now, resp)
        self._promote(path, datatype, resp)
        return resp

    def _promote(self, path: str, datatype: str, resp: List[Dict[str, Any]]) -> None:
        """Start streaming a pending path once its first data shows up.

        A path that was empty when the report was opened is served by these
        TTL-cached Gets, so the response that finally carries data is also the
        one that reveals the envelope key. Learning it here means the path joins
        the subscription without spending a Get of its own on probing.
        """
        with self._lock:
            state = self._paths.get(path)
            if (
                state is None
                or state.bootstrapped
                or not state.streamable
                or state.spec.datatype != datatype
            ):
                return
            spec = state.spec
        if self._absorb(spec, resp, self._tree):
            logger.info("%s: %s now has state, subscribing to it", self.name, path)
            self._dirty.set()

    def _raw_get(self, path: str, datatype: str) -> List[Dict[str, Any]]:
        # Serialized on purpose: an in-flight Get holds a gRPC session on the
        # target just like the subscription does, and the node's budget is
        # shared with every other gRPC client.
        with self._get_lock:
            self._gets += 1
            resp = self.device.get(paths=[path], datatype=datatype)
        return [strip_modules(d) for d in resp] if resp else []

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, Any]:
        with self._lock:
            paths = [
                {
                    "path": state.spec.path,
                    "mode": state.spec.mode,
                    "sample_interval": state.spec.sample_interval,
                    "streaming": state.streamable and state.bootstrapped,
                    # Empty when the report was opened, so the envelope shape is
                    # not known yet; served by TTL-cached Gets until it fills up.
                    "pending": state.streamable and not state.bootstrapped,
                    "error": state.error,
                }
                for state in self._paths.values()
            ]
        return {
            "node": self.name,
            "connected": self.connected,
            "error": self.error,
            "last_update": self.last_update,
            # gRPC sessions this node currently spends on us: the Subscribe RPC
            # plus at most one in-flight Get.
            "sessions": (1 if self.connected else 0)
            + (1 if self._get_lock.locked() else 0),
            "gets": self._gets,
            "paths": paths,
        }


def _key_hints(path: str) -> Dict[str, List[str]]:
    """Map YANG list name -> key leaves, as far as the requested path reveals.

    A gNMI ``Get`` response returns lists inline without marking which leaves
    are the keys. The requested path does name them (``interface[name=*]``), so
    those hints let the bootstrap store the list as an addressable list node
    instead of an opaque blob.
    """
    hints: Dict[str, List[str]] = {}
    for name, keys in parse_path(path):
        if keys:
            hints[name] = list(keys)
    return hints


def _touched_interface(path: str) -> Optional[str]:
    """Return the interface name when *path* points into interface statistics."""
    elems = parse_path(path)
    if len(elems) < 2 or elems[0][0] != "interface":
        return None
    name = elems[0][1].get("name")
    if not name:
        return None
    if not any(elem[0] == "statistics" for elem in elems[1:]):
        return None
    return name
