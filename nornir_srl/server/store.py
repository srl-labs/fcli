"""Fabric-wide state: gNMI connections, subscriptions and rendered tables."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from nornir.core import Nornir

from ..connections.srlinux import CONNECTION_NAME
from ..reports import ReportSpec, SubscriptionSpec, get_report
from ..rows import clean_columns, flatten
from .devices import CachedDevice, RecordingDevice
from .stream import HostStream

logger = logging.getLogger(__name__)


class FabricStore:
    """Owns one :class:`HostStream` per node and renders reports from them."""

    def __init__(
        self,
        nornir: Nornir,
        *,
        sample_interval: Optional[int] = None,
        resync_interval: int = 300,
        workers: int = 20,
        restart_debounce: float = 1.0,
        idle_timeout: float = 900.0,
        connect_retry_interval: float = 30.0,
        topo_name: Optional[str] = None,
    ) -> None:
        self.nornir = nornir
        self.topo_name = topo_name
        self.sample_interval = sample_interval
        self.resync_interval = resync_interval
        self.restart_debounce = restart_debounce
        self.idle_timeout = idle_timeout
        self.connect_retry_interval = connect_retry_interval
        self._pool = ThreadPoolExecutor(
            max_workers=max(workers, 1), thread_name_prefix="fcli-srv"
        )
        self._streams: Dict[str, HostStream] = {}
        self._connect_errors: Dict[str, str] = {}
        #: When each unconnected node was last attempted, to rate-limit retries.
        self._connect_attempts: Dict[str, float] = {}
        #: Discovered paths per (node, report). Discovery runs the report getter
        #: against the live device, so its result is cached; re-asserting the
        #: paths themselves is cheap and happens on every render.
        self._specs: Dict[Tuple[str, str], List[SubscriptionSpec]] = {}
        #: Why a node could not serve a report, and when that was decided.
        self._activation_errors: Dict[Tuple[str, str], Tuple[float, str]] = {}
        #: Centralized table cache per (report_name, inv_filter_tuple) -> (timestamp, table)
        self._table_cache: Dict[
            Tuple[str, Optional[Tuple[Tuple[str, str], ...]]],
            Tuple[float, Dict[str, Any]],
        ] = {}
        #: Timestamp of the most recent fabric state update or topology change.
        self._last_state_change: float = time.time()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._resync_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def _on_host_update(self) -> None:
        """Callback invoked when a HostStream receives a telemetry update."""
        with self._lock:
            self._last_state_change = time.time()

    def start(self) -> None:
        """Open a gNMI connection to every node in the inventory."""
        hosts = list(self.nornir.inventory.hosts.items())
        futures = [self._pool.submit(self._connect, name, host) for name, host in hosts]
        for fut in futures:
            while not self._stop.is_set():
                try:
                    fut.result(timeout=0.1)
                    break
                except TimeoutError:
                    continue
                except Exception:
                    break
        connected = len(self._streams)
        logger.info("connected to %d/%d node(s)", connected, len(hosts))
        if self.resync_interval > 0:
            self._resync_thread = threading.Thread(
                target=self._resync_loop, name="fcli-resync", daemon=True
            )
            self._resync_thread.start()

    def _connect(self, name: str, host: Any) -> None:
        if self._stop.is_set():
            # Opening one now would hold a session on the node with nothing left
            # to close it: connects are queued, so they can outlive the store.
            return
        with self._lock:
            self._connect_attempts[name] = time.time()
        try:
            device = host.get_connection(CONNECTION_NAME, self.nornir.config)
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.warning("%s: connection failed: %s", name, exc)
            with self._lock:
                self._connect_errors[name] = str(exc)
                self._last_state_change = time.time()
            return
        with self._lock:
            if self._stop.is_set():
                # Lost the race with stop(). Keeping this would leave a session
                # open on the node with nothing left to close it.
                stopping = True
            else:
                stopping = False
                self._connect_errors.pop(name, None)
                self._streams[name] = HostStream(
                    name,
                    device,
                    default_sample_interval=self.sample_interval or 15,
                    restart_debounce=self.restart_debounce,
                    idle_timeout=self.idle_timeout,
                    on_update=self._on_host_update,
                )
                self._last_state_change = time.time()
        if stopping:
            try:
                host.close_connection(CONNECTION_NAME)
            except Exception as exc:  # noqa: BLE001 - best effort teardown
                logger.debug("%s: closing a late connection failed: %s", name, exc)

    def _heal_connections(self, names: List[str]) -> None:
        """Reconnect the nodes among *names* whose gNMI connection does not work.

        Two situations end up here. A node that was unreachable when the server
        started has no connection at all, because opening one reaches the node
        to fetch its TLS certificate. A node that went away afterwards - one
        that rebooted, or a whole lab that was redeployed - does have one, but
        its gRPC channel belongs to the instance that disappeared and keeps
        answering every call from its own failed state, so it has to be replaced
        rather than waited on.

        Both are rate-limited to one attempt per ``connect_retry_interval`` and
        run in the background: the render that schedules one still reports the
        node as failing, and a later render picks it up. That keeps a node that
        is slow to fail from holding up the render of every other node.
        """
        if self._stop.is_set():
            return
        now = time.time()
        due: List[str] = []
        with self._lock:
            for name in names:
                stream = self._streams.get(name)
                if stream is not None:
                    failing = stream.failing_since
                    # Updates that stopped arriving count as much as calls that
                    # fail: a silently dead subscription is only recovered by a
                    # new connection, since nothing about it looks broken.
                    stale = stream.stale_for
                    if failing is None and stale is None:
                        continue  # the node is answering
                    bad_for = max(
                        now - failing if failing is not None else 0.0, stale or 0.0
                    )
                    if self.connect_retry_interval > 0 and bad_for < self.connect_retry_interval:
                        continue
                last = self._connect_attempts.get(name)
                if self.connect_retry_interval > 0 and last is not None and 0 <= (now - last) < self.connect_retry_interval:
                    continue
                # Stamped before submitting, so concurrent renders queue a node
                # once rather than once each.
                self._connect_attempts[name] = now
                due.append(name)
        for name in due:
            host = self.nornir.inventory.hosts.get(name)
            if host is not None:
                self._pool.submit(self._reconnect, name, host)

    def _reconnect(self, name: str, host: Any) -> None:
        """Give *name* a fresh gNMI connection, discarding anything stale."""
        with self._lock:
            stream = self._streams.pop(name, None)
            # Both were learned through the connection that stopped working, so
            # they are re-discovered against the new one.
            for key in [k for k in self._specs if k[0] == name]:
                del self._specs[key]
            for key in [k for k in self._activation_errors if k[0] == name]:
                del self._activation_errors[key]
            self._table_cache.clear()
            self._last_state_change = time.time()
        if stream is not None:
            logger.info("%s: gNMI calls stopped working, reconnecting", name)
            stream.stop()
            try:
                host.close_connection(CONNECTION_NAME)
            except Exception as exc:  # noqa: BLE001 - best effort teardown
                logger.debug("%s: closing the old connection failed: %s", name, exc)
        self._connect(name, host)

    def _resync_loop(self) -> None:
        """Re-read one node per tick, spreading a sweep over ``resync_interval``.

        SAMPLE subscriptions refresh the values they carry but rely on the target
        reporting deletes for entries that disappear, so a periodic full re-read
        is the safety net. It costs one ``Get`` per subscribed path, which is
        why nodes are walked round-robin rather than all at once.
        """
        cursor = 0
        while True:
            with self._lock:
                node_count = len(self._streams)
            if self._stop.wait(self.resync_interval / max(node_count, 1)):
                return
            with self._lock:
                names = list(self._streams)
                if not names:
                    continue
                stream = self._streams.get(names[cursor % len(names)])
            cursor += 1
            if stream is None:
                continue
            try:
                stream.resync()
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.debug("%s: resync failed: %s", stream.name, exc)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._table_cache.clear()
            # Handed over rather than read: a connect still queued on the pool
            # would otherwise register a fresh stream behind our back, and
            # nothing would be left to shut it down again.
            streams = list(self._streams.values())
            self._streams.clear()
        if self._resync_thread is not None:
            self._resync_thread.join(timeout=5)

        hosts = list(self.nornir.inventory.hosts.items())

        def _close_host(item: Tuple[str, Any]) -> None:
            name, host = item
            try:
                host.close_connections()
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s: error closing connection: %s", name, exc)

        def _stop_stream(stream: Any) -> None:
            try:
                stream.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s: error stopping stream: %s", stream.name, exc)

        # Streams first: stopping one cancels its Subscribe RPC, which is what
        # actually releases the session on the node.
        list(self._pool.map(_stop_stream, streams))
        list(self._pool.map(_close_host, hosts))
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ #
    # inventory
    # ------------------------------------------------------------------ #

    def inventory(self) -> List[Dict[str, Any]]:
        with self._lock:
            streams = dict(self._streams)
            connect_errors = dict(self._connect_errors)
        result = []
        for name, host in self.nornir.inventory.hosts.items():
            stream = streams.get(name)
            result.append(
                {
                    "name": name,
                    "hostname": host.hostname or name,
                    "labels": {k: v for k, v in (host.data or {}).items()},
                    # 'connected' says the node is answering: holding a stream
                    # object proves nothing, since the gRPC channel behind it
                    # outlives the node it was opened to. 'streaming' says a
                    # Subscribe RPC is running on it, which only happens once a
                    # report has been opened.
                    # Three different ways of losing a node, because no single
                    # one of them catches the others: the subscription reporting
                    # an error, a Get failing or hanging, and updates that were
                    # due never arriving on a connection nobody declared dead.
                    "connected": bool(
                        stream
                        and stream.error is None
                        and stream.failing_since is None
                        and stream.stale_for is None
                    ),
                    "streaming": bool(stream and stream.connected),
                    "error": connect_errors.get(name)
                    or (stream.last_error if stream else None),
                    "last_update": stream.last_update if stream else None,
                }
            )
        return result

    def _targets(self, inv_filter: Optional[Dict[str, str]]) -> List[str]:
        target = self.nornir.filter(**inv_filter) if inv_filter else self.nornir
        return list(target.inventory.hosts)

    def _streams_for(self, names: List[str]) -> List[HostStream]:
        with self._lock:
            return [self._streams[n] for n in names if n in self._streams]

    # ------------------------------------------------------------------ #
    # report activation
    # ------------------------------------------------------------------ #

    def activate(self, report: ReportSpec, hosts: Optional[List[str]] = None) -> None:
        """Make sure every node streams the paths *report* needs.

        This runs on every render, not just the first one: re-asserting the paths
        is what marks them as still in use, so a report someone is watching is
        never retired from under it.
        """
        with self._lock:
            names = hosts if hosts is not None else list(self._streams)
            pending = [n for n in names if n in self._streams]
        if not pending:
            return
        list(self._pool.map(lambda n: self._activate_host(report, n), pending))

    def _activate_host(self, report: ReportSpec, name: str) -> None:
        key = (name, report.name)
        with self._lock:
            stream = self._streams.get(name)
            if stream is None:
                return
            failed = self._activation_errors.get(key)
            if failed is not None:
                # Discovery runs the report's getter against the device, so a
                # node that cannot serve it is not re-probed on every render.
                # The reason is often temporary though - the node was rebooting
                # - so the verdict expires instead of standing for good.
                if time.time() - failed[0] < self.connect_retry_interval:
                    return
                del self._activation_errors[key]
            specs = self._specs.get(key)
        try:
            if specs is None:
                specs = self._discover(report, stream)
                with self._lock:
                    self._specs[key] = specs
            stream.ensure_paths(specs)
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.warning(
                "%s: activating report '%s' failed: %s", name, report.name, exc
            )
            with self._lock:
                self._activation_errors[key] = (time.time(), str(exc))

    def _discover(self, report: ReportSpec, stream: HostStream) -> List[SubscriptionSpec]:
        """Determine which gNMI paths a report needs on this node."""
        if report.subscribe:
            interval = self.sample_interval
            if interval is None:
                return list(report.subscribe)
            return [
                SubscriptionSpec(
                    s.path, s.datatype, s.mode, min(interval, s.sample_interval)
                )
                for s in report.subscribe
            ]
        recorder = RecordingDevice(stream.device, stream.discovery_get)
        report.getter(recorder)
        interval = self.sample_interval or report.sample_interval
        return [
            SubscriptionSpec(path=path, datatype=datatype, sample_interval=interval)
            for path, datatype in recorder.recorded
        ]

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #

    def table(
        self,
        report: ReportSpec,
        inv_filter: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Render *report* across the (filtered) inventory from streamed state."""
        names = self._targets(inv_filter)
        self._heal_connections(names)
        self.activate(report, names)

        inv_key = tuple(sorted(inv_filter.items())) if inv_filter else None
        cache_key = (report.name, inv_key)
        now = time.time()
        with self._lock:
            cached = self._table_cache.get(cache_key)
            if cached is not None:
                cached_at, cached_table = cached
                if (cached_at >= self._last_state_change or (now - cached_at < 0.5)) and not cached_table.get("errors"):
                    return cached_table

        started = time.time()
        results = list(self._pool.map(lambda n: self._host_rows(report, n), names))

        columns: List[str] = []
        rows: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for name, cols, host_rows, error in results:
            if error:
                errors.append({"node": name, "error": error})
                continue
            if not columns and cols:
                columns = cols
            rows.extend(host_rows)

        all_columns = ["Node"] + clean_columns(columns)
        clean_rows = [
            {c: _cell(row.get(raw)) for c, raw in zip(all_columns, ["Node"] + columns)}
            for row in rows
        ]
        res_table = {
            "report": report.name,
            "title": report.title,
            "columns": all_columns,
            "rows": clean_rows,
            "errors": errors,
            "nodes": len(names),
            "generated": started,
            "render_ms": round((time.time() - started) * 1000, 1),
            "oldest_update": _oldest_update(self._streams_for(names)),
        }
        with self._lock:
            # The cache is keyed by inventory filter as well as report, and API
            # clients pick the filter, so the key space has no natural bound.
            if (
                cache_key not in self._table_cache
                and len(self._table_cache) >= _MAX_CACHED_TABLES
            ):
                oldest = min(self._table_cache, key=lambda k: self._table_cache[k][0])
                del self._table_cache[oldest]
            self._table_cache[cache_key] = (started, res_table)
        return res_table

    def _host_rows(
        self, report: ReportSpec, name: str
    ) -> Tuple[str, List[str], List[Dict[str, Any]], Optional[str]]:
        with self._lock:
            stream = self._streams.get(name)
            if stream is None:
                return name, [], [], self._connect_errors.get(name, "not connected")
            activation_error = self._activation_errors.get((name, report.name))
        if activation_error:
            return name, [], [], activation_error[1]
        host = self.nornir.inventory.hosts.get(name)
        node = (host.hostname if host and host.hostname else name) or name
        try:
            result = report.getter(CachedDevice(stream))
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.debug("%s: report '%s' failed: %s", name, report.name, exc)
            return name, [], [], str(exc)
        items = (result or {}).get(report.resource) or []
        columns, rows = flatten(node, items)
        return name, columns, rows, None

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, Any]:
        with self._lock:
            host_streams = list(self._streams.values())
            connect_errors = dict(self._connect_errors)
        streams = [s.status() for s in host_streams]
        return {
            "nodes": streams,
            "unreachable": [{"node": n, "error": e} for n, e in connect_errors.items()],
            "subscriptions": sum(len(s["paths"]) for s in streams),
            # The gRPC sessions a single node spends on us. SR Linux allows 20
            # per gRPC server by default, shared with every other client, so
            # this is the number to watch.
            "max_sessions_per_node": max((s["sessions"] for s in streams), default=0),
            "resync_interval": self.resync_interval,
        }

    def overview(self, inv_filter: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Aggregate fabric-wide health and topology metrics for the dashboard."""
        names = self._targets(inv_filter)
        self._heal_connections(names)
        try:
            self.activate(get_report("overview"), names)
        except Exception as exc:  # noqa: BLE001 - the summary still renders
            logger.warning("activating the overview report failed: %s", exc)

        hosts = self.inventory()
        with self._lock:
            streams = [self._streams[n] for n in names if n in self._streams]
            unreachable_nodes = len(self._connect_errors)
            cached_tables = len(self._table_cache)
            all_streams = list(self._streams.values())

        # Each snapshot is taken under its own node's lock and nothing else, so
        # summarizing the fabric does not stall the renders of every report on it.
        health = _Health()
        for stream in streams:
            snapshot = stream.snapshot_roots(_OVERVIEW_ROOTS)
            itfs = snapshot.get("interface")
            _tally_interfaces(health, itfs)
            _tally_network_instances(health, snapshot.get("network-instance"), itfs)

        return {
            "nodes": {
                "total": len(hosts),
                "connected": sum(1 for h in hosts if h["connected"]),
                "streaming": sum(1 for h in hosts if h["streaming"]),
                "unreachable": unreachable_nodes,
            },
            "bgp": {
                "total": health.bgp_total,
                "established": health.bgp_established,
                "down": health.bgp_down,
            },
            "interfaces": {
                "total": health.itf_total,
                "down": health.itf_down,
                "errors": health.itf_errors,
            },
            "bridge_domains": _roll_up(health.bridge_domains),
            "routers": _roll_up(health.routers),
            "telemetry": {
                "subscriptions": sum(len(s.status()["paths"]) for s in all_streams),
                "resync_interval": self.resync_interval,
                "cached_tables": cached_tables,
            },
        }


#: Tree roots the overview summarizes.
_OVERVIEW_ROOTS: Tuple[str, ...] = ("interface", "network-instance")

#: Cap on cached rendered tables, evicting the oldest beyond it.
_MAX_CACHED_TABLES = 256

_UP_STATES = frozenset({"up", "enable", "enabled", "active"})
_DOWN_STATES = frozenset({"down", "disable", "disabled"})
#: A network-instance reports itself established rather than up.
_MEMBER_UP_STATES = _UP_STATES | {"established"}

_ERROR_COUNTERS: Tuple[str, ...] = (
    "in-error-packets",
    "out-error-packets",
    "in-discarded-packets",
    "out-discarded-packets",
)


@dataclass
class _Health:
    """Tallies accumulated across the nodes of the fabric."""

    bgp_total: int = 0
    bgp_established: int = 0
    bgp_down: int = 0
    itf_total: int = 0
    itf_down: int = 0
    itf_errors: int = 0
    #: (service name, state) per node, rolled up by :func:`_roll_up`.
    bridge_domains: List[Tuple[str, str]] = field(default_factory=list)
    routers: List[Tuple[str, str]] = field(default_factory=list)


def _leaf(value: Any) -> str:
    """Normalize a YANG enum leaf to its bare, lower-case value."""
    if not value:
        return ""
    return str(value).lower().split(":")[-1]


def _is_configured(itf: Dict[str, Any], oper_state: str) -> bool:
    """Whether an interface is in use, and so worth counting as healthy or not.

    Most ports of a fabric leaf are never patched, and an unused port is down by
    definition; counting those as faults would bury the ones that matter. A port
    counts once anything says it is meant to carry traffic.
    """
    name = str(itf.get("name", ""))
    if name.startswith(("mgmt", "system", "lo", "lag")):
        return True
    if oper_state == "up" or itf.get("description"):
        return True
    subinterfaces = itf.get("subinterface", [])
    if subinterfaces:
        return True
    ethernet = itf.get("ethernet", {})
    return isinstance(ethernet, dict) and bool(ethernet.get("aggregate-id"))


def _tally_interfaces(health: _Health, itfs: Any) -> None:
    """Count the configured interfaces of one node by health."""
    if not isinstance(itfs, list):
        return
    for itf in itfs:
        if not isinstance(itf, dict):
            continue
        if _leaf(itf.get("admin-state")) in _DOWN_STATES:
            continue
        oper_state = _leaf(itf.get("oper-state"))
        if not _is_configured(itf, oper_state):
            continue
        health.itf_total += 1
        if oper_state == "down":
            health.itf_down += 1
        stats = itf.get("statistics", {})
        if not isinstance(stats, dict):
            continue
        errors = 0
        for counter in _ERROR_COUNTERS:
            try:
                errors += int(stats.get(counter, 0) or 0)
            except (TypeError, ValueError):
                continue
        if errors > 0:
            health.itf_errors += 1


def _route_targets(ni: Dict[str, Any]) -> List[str]:
    """The route-targets of a network-instance, as ``target:x:y`` strings."""
    instances = _branch(ni, "protocols", "bgp-vpn").get("bgp-instance", [])
    if isinstance(instances, dict):
        instances = [instances]
    if not isinstance(instances, list):
        return []
    targets = set()
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        config = instance.get("route-target", {})
        if not isinstance(config, dict):
            continue
        for key in ("import-rt", "export-rt"):
            raw = config.get(key, [])
            if isinstance(raw, (str, dict)):
                raw = [raw]
            if not isinstance(raw, list):
                continue
            for item in raw:
                target = item.get("target") if isinstance(item, dict) else item
                if not target:
                    continue
                text = str(target)
                targets.add(text if text.startswith("target:") else f"target:{text}")
    return sorted(targets)


def _branch(node: Any, *names: str) -> Dict[str, Any]:
    """Descend through nested containers, yielding ``{}`` at the first miss."""
    for name in names:
        if not isinstance(node, dict):
            return {}
        node = node.get(name, {})
    return node if isinstance(node, dict) else {}


def _effective_state(ni: Dict[str, Any], oper_state: str, itf_states: Dict[str, str]) -> str:
    """The state of a network-instance, refined by the interfaces attached to it.

    A network-instance reports itself up while some of the interfaces placed in
    it are down, which is what 'degraded' is for: the service exists on the node
    but is not carrying everything it was meant to.
    """
    if oper_state == "down":
        return "down"
    attached = ni.get("interface", [])
    if not isinstance(attached, list):
        return oper_state
    states = [
        state
        for state in (
            itf_states.get(str(itf.get("name", "")))
            for itf in attached
            if isinstance(itf, dict) and itf.get("name")
        )
        if state
    ]
    if not states:
        return oper_state
    if all(state in _UP_STATES for state in states):
        return "up"
    if all(state in _DOWN_STATES for state in states):
        return "down"
    return "degraded"


def _tally_network_instances(health: _Health, nis: Any, itfs: Any) -> None:
    """Count BGP sessions and record the services of one node."""
    if not isinstance(nis, list):
        return
    itf_states: Dict[str, str] = {}
    if isinstance(itfs, list):
        for itf in itfs:
            if isinstance(itf, dict) and itf.get("name"):
                state = _leaf(itf.get("oper-state"))
                if state:
                    itf_states[str(itf["name"])] = state
    for ni in nis:
        if not isinstance(ni, dict):
            continue
        name = str(ni.get("name", ""))
        ni_type = _leaf(ni.get("type"))
        oper_state = _leaf(ni.get("oper-state")) or "unknown"

        neighbors = _branch(ni, "protocols", "bgp").get("neighbor", [])
        if isinstance(neighbors, list):
            for neighbor in neighbors:
                if not isinstance(neighbor, dict):
                    continue
                health.bgp_total += 1
                if _leaf(neighbor.get("session-state")) == "established":
                    health.bgp_established += 1
                else:
                    health.bgp_down += 1

        state = _effective_state(ni, oper_state, itf_states)
        targets = _route_targets(ni)
        # A service spans nodes, and its route-target is what identifies it
        # across them; the local name is only a fallback for an unnamed one.
        if ni_type == "mac-vrf":
            health.bridge_domains.append(
                (targets[0] if targets else f"mac-vrf:{name}", state)
            )
        elif ni_type in ("ip-vrf", "vrf") and name != "mgmt":
            health.routers.append(
                (targets[0] if targets else f"ip-vrf:{name}", state)
            )


def _roll_up(instances: List[Tuple[str, str]]) -> Dict[str, int]:
    """Group per-node service instances into fabric-wide service health."""
    by_name: Dict[str, List[str]] = {}
    for name, state in instances:
        by_name.setdefault(name, []).append(state)
    up = degraded = down = 0
    for states in by_name.values():
        up_count = sum(1 for s in states if s in _MEMBER_UP_STATES)
        if up_count == len(states):
            up += 1
        elif up_count == 0:
            down += 1
        else:
            degraded += 1
    return {
        "total": len(by_name),
        "up": up,
        "degraded": degraded,
        "down": down,
        "instances": len(instances),
    }


def _cell(value: Any) -> Any:
    """Render one value into something JSON- and table-friendly."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _oldest_update(streams: List[HostStream]) -> Optional[float]:
    stamps = [s.last_update for s in streams if s.last_update]
    return min(stamps) if stamps else None
