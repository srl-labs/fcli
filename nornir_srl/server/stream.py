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
from .tree import delete, get_node, insert, join_path, materialize, parse_path

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
    """Streaming state for a single SR Linux node."""

    def __init__(
        self,
        name: str,
        device: Any,
        *,
        default_sample_interval: int = 10,
        get_ttl: float = 30.0,
        reconnect_delay: float = 5.0,
    ) -> None:
        self.name = name
        self.device = device
        self.default_sample_interval = default_sample_interval
        self.get_ttl = get_ttl
        self.reconnect_delay = reconnect_delay

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
        self._generation = 0
        self._subscription: Any = None
        self.last_update: Optional[float] = None
        self.connected = False
        self.error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # subscription lifecycle
    # ------------------------------------------------------------------ #

    def ensure_paths(self, specs: List[SubscriptionSpec]) -> None:
        """Make sure every spec in *specs* is subscribed, restarting if needed."""
        added = []
        with self._lock:
            for spec in specs:
                if spec.path in self._paths:
                    continue
                self._paths[spec.path] = PathState(spec=spec)
                added.append(spec)
        if not added:
            return
        for spec in added:
            self._bootstrap(spec, self._tree)
        self._restart()

    def _bootstrap(self, spec: SubscriptionSpec, tree: Dict[str, Any]) -> None:
        """Seed *tree* with a gNMI Get and learn the response envelope keys."""
        state = self._paths[spec.path]
        try:
            resp = self._raw_get(spec.path, spec.datatype)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            state.error = str(exc)
            state.streamable = False
            logger.warning(
                "%s: bootstrap Get failed for %s: %s", self.name, spec.path, exc
            )
            return
        envelopes: List[str] = []
        streamable = True
        hints = _key_hints(spec.path)
        for item in resp:
            if not isinstance(item, dict) or len(item) != 1:
                # Keyless yang-list responses cannot be placed in the tree;
                # such a path is served by a TTL-cached Get instead.
                streamable = False
                envelopes = []
                break
            env_key = next(iter(item))
            env_path = "" if env_key in ("/", "") else env_key
            insert(tree, env_path, item[env_key], key_hints=hints)
            if env_path not in envelopes:
                envelopes.append(env_path)
        with self._lock:
            state.streamable = streamable
            if streamable:
                state.envelopes = envelopes
                state.bootstrapped = True
                state.error = None

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

    def _restart(self) -> None:
        """(Re)start the subscription thread with the current path set."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            specs = [
                s.spec for s in self._paths.values() if s.streamable and s.bootstrapped
            ]
        self._close_subscription()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if not specs:
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
        while not self._stop.is_set() and generation == self._generation:
            try:
                subscription = self.device.gnmi_subscribe(request)
                self._subscription = subscription
                self.connected = True
                self.error = None
                logger.info("%s: subscribed to %d path(s)", self.name, len(specs))
                while not self._stop.is_set() and generation == self._generation:
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
            if self._stop.is_set() or generation != self._generation:
                break
            self._stop.wait(self.reconnect_delay)
        self.connected = False

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._generation += 1
        self._close_subscription()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

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
            result = []
            for env in state.envelopes:
                node = self._tree if env == "" else get_node(self._tree, env)
                key = env if env else "/"
                result.append({key: materialize(node) if node is not None else {}})
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
        return resp

    def _raw_get(self, path: str, datatype: str) -> List[Dict[str, Any]]:
        with self._get_lock:
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
                    "error": state.error,
                }
                for state in self._paths.values()
            ]
        return {
            "node": self.name,
            "connected": self.connected,
            "error": self.error,
            "last_update": self.last_update,
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
