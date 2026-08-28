"""Fabric-wide state: gNMI connections, subscriptions and rendered tables."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from nornir.core import Nornir

from ..connections.srlinux import CONNECTION_NAME
from .devices import CachedDevice, RecordingDevice
from .reports import Report
from .rows import clean_columns, flatten
from .stream import HostStream, SubscriptionSpec
from .tree import materialize

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
            names = list(self._streams)
            if self._stop.wait(self.resync_interval / max(len(names), 1)):
                return
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

        hosts = list(self.nornir.inventory.hosts.items())
        streams = list(self._streams.values())

        def _close_host(item: Tuple[str, Any]) -> None:
            name, host = item
            try:
                host.close_connections()
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s: error closing connection: %s", name, exc)

        def _stop_stream(stream: Any) -> None:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001
                pass

        list(self._pool.map(_close_host, hosts))
        list(self._pool.map(_stop_stream, streams))
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ #
    # inventory
    # ------------------------------------------------------------------ #

    def inventory(self) -> List[Dict[str, Any]]:
        result = []
        for name, host in self.nornir.inventory.hosts.items():
            stream = self._streams.get(name)
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
                    "error": self._connect_errors.get(name)
                    or (stream.last_error if stream else None),
                    "last_update": stream.last_update if stream else None,
                }
            )
        return result

    def _targets(self, inv_filter: Optional[Dict[str, str]]) -> List[str]:
        target = self.nornir.filter(**inv_filter) if inv_filter else self.nornir
        return list(target.inventory.hosts)

    # ------------------------------------------------------------------ #
    # report activation
    # ------------------------------------------------------------------ #

    def activate(self, report: Report, hosts: Optional[List[str]] = None) -> None:
        """Make sure every node streams the paths *report* needs.

        This runs on every render, not just the first one: re-asserting the paths
        is what marks them as still in use, so a report someone is watching is
        never retired from under it.
        """
        names = hosts if hosts is not None else list(self._streams)
        pending = [n for n in names if n in self._streams]
        if not pending:
            return
        list(self._pool.map(lambda n: self._activate_host(report, n), pending))

    def _activate_host(self, report: Report, name: str) -> None:
        stream = self._streams.get(name)
        if stream is None:
            return
        key = (name, report.name)
        with self._lock:
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

    def _discover(self, report: Report, stream: HostStream) -> List[SubscriptionSpec]:
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
        report: Report,
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
            "oldest_update": _oldest_update(
                [self._streams[n] for n in names if n in self._streams]
            ),
        }
        with self._lock:
            self._table_cache[cache_key] = (started, res_table)
        return res_table

    def _host_rows(
        self, report: Report, name: str
    ) -> Tuple[str, List[str], List[Dict[str, Any]], Optional[str]]:
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
        streams = [s.status() for s in self._streams.values()]
        return {
            "nodes": streams,
            "unreachable": [
                {"node": n, "error": e} for n, e in self._connect_errors.items()
            ],
            "subscriptions": sum(len(s["paths"]) for s in streams),
            # The gRPC sessions a single node spends on us. SR Linux allows 20
            # per gRPC server by default, shared with every other client, so
            # this is the number to watch.
            "max_sessions_per_node": max((s["sessions"] for s in streams), default=0),
            "resync_interval": self.resync_interval,
        }

    def overview(self, inv_filter: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Aggregate high-level health and topology metrics for executive dashboard."""
        from .reports import get_report

        names = self._targets(inv_filter)
        self._heal_connections(names)
        try:
            ov_report = get_report("overview")
            self.activate(ov_report, names)
        except Exception:
            pass

        hosts = self.inventory()
        total_nodes = len(hosts)
        connected_nodes = sum(1 for h in hosts if h["connected"])
        streaming_nodes = sum(1 for h in hosts if h["streaming"])
        unreachable_nodes = len(self._connect_errors)

        bgp_total = 0
        bgp_established = 0
        bgp_down = 0

        itf_total = 0
        itf_down = 0
        itf_errors = 0

        bd_instances_list: List[Dict[str, Any]] = []
        router_instances_list: List[Dict[str, Any]] = []

        def _clean_val(v: Any) -> str:
            if not v:
                return ""
            s = str(v).lower()
            return s.split(":")[-1]

        with self._lock:
            for name in names:
                stream = self._streams.get(name)
                if stream is None:
                    continue
                with stream._lock:
                    itf_node = stream._tree.get("interface")
                    itfs = materialize(itf_node) if itf_node is not None else None
                    ni_node = stream._tree.get("network-instance")
                    nis = materialize(ni_node) if ni_node is not None else None

                if itfs is not None:
                    if isinstance(itfs, list):
                        for item in itfs:
                            if isinstance(item, dict):
                                admin = _clean_val(item.get("admin-state"))
                                if admin in ("disable", "disabled"):
                                    continue
                                subitfs = item.get("subinterface", [])
                                has_subitfs = len(subitfs) > 0 if isinstance(subitfs, list) else bool(subitfs)
                                has_desc = bool(item.get("description"))
                                eth_cfg = item.get("ethernet", {})
                                has_lag = bool(eth_cfg.get("aggregate-id")) if isinstance(eth_cfg, dict) else False
                                itf_name = str(item.get("name", ""))
                                is_sys = itf_name.startswith(("mgmt", "system", "lo", "lag"))
                                oper_st = _clean_val(item.get("oper-state"))

                                is_configured = (
                                    oper_st == "up"
                                    or is_sys
                                    or has_subitfs
                                    or has_desc
                                    or has_lag
                                )
                                if not is_configured:
                                    continue
                                itf_total += 1
                                if oper_st == "down":
                                    itf_down += 1
                                stats = item.get("statistics", {})
                                if isinstance(stats, dict):
                                    err_cnt = (
                                        int(stats.get("in-error-packets", 0) or 0)
                                        + int(stats.get("out-error-packets", 0) or 0)
                                        + int(stats.get("in-discarded-packets", 0) or 0)
                                        + int(stats.get("out-discarded-packets", 0) or 0)
                                    )
                                    if err_cnt > 0:
                                        itf_errors += 1

                if nis is not None:
                    if isinstance(nis, list):
                        for ni in nis:
                            if not isinstance(ni, dict):
                                continue
                            ni_name = str(ni.get("name", ""))
                            ni_type = _clean_val(ni.get("type"))
                            oper_state = _clean_val(ni.get("oper-state")) or "unknown"

                            bgp = ni.get("protocols", {}).get("bgp", {})
                            for nbr in bgp.get("neighbor", []):
                                if isinstance(nbr, dict):
                                    bgp_total += 1
                                    state = _clean_val(nbr.get("session-state"))
                                    if state == "established":
                                        bgp_established += 1
                                    else:
                                        bgp_down += 1

                            bgp_vpn = ni.get("protocols", {}).get("bgp-vpn", {})
                            bgp_instances = bgp_vpn.get("bgp-instance", [])
                            if isinstance(bgp_instances, dict):
                                bgp_instances = [bgp_instances]
                            rts = set()
                            for inst in bgp_instances:
                                if isinstance(inst, dict):
                                    rt_cfg = inst.get("route-target", {})
                                    if isinstance(rt_cfg, dict):
                                        for key in ("import-rt", "export-rt"):
                                            rts_raw = rt_cfg.get(key, [])
                                            if isinstance(rts_raw, (str, dict)):
                                                rts_raw = [rts_raw]
                                            for item in rts_raw:
                                                target = item.get("target") if isinstance(item, dict) else item
                                                if target:
                                                    t_str = str(target)
                                                    if not t_str.startswith("target:"):
                                                        t_str = f"target:{t_str}"
                                                    rts.add(t_str)
                            rt_list = sorted(list(rts))

                            if ni_type == "mac-vrf":
                                primary_bd = rt_list[0] if rt_list else f"mac-vrf:{ni_name}"
                                bd_instances_list.append({
                                    "name": primary_bd,
                                    "oper_state": oper_state,
                                })
                            elif ni_type in ("ip-vrf", "vrf") and ni_name != "mgmt":
                                primary_router = rt_list[0] if rt_list else f"ip-vrf:{ni_name}"
                                router_instances_list.append({
                                    "name": primary_router,
                                    "oper_state": oper_state,
                                })

        # Aggregate Bridge Domains
        bd_map: Dict[str, List[str]] = {}
        for item in bd_instances_list:
            bd_map.setdefault(item["name"], []).append(item["oper_state"])

        bd_up = 0
        bd_degraded = 0
        bd_down = 0
        for _bd_name, st_list in bd_map.items():
            up_cnt = sum(1 for s in st_list if s in ("up", "enable", "enabled", "active", "established"))
            if up_cnt == len(st_list):
                bd_up += 1
            elif up_cnt == 0:
                bd_down += 1
            else:
                bd_degraded += 1

        # Aggregate Routers
        router_map: Dict[str, List[str]] = {}
        for item in router_instances_list:
            router_map.setdefault(item["name"], []).append(item["oper_state"])

        r_up = 0
        r_degraded = 0
        r_down = 0
        for _r_name, st_list in router_map.items():
            up_cnt = sum(1 for s in st_list if s in ("up", "enable", "enabled", "active", "established"))
            if up_cnt == len(st_list):
                r_up += 1
            elif up_cnt == 0:
                r_down += 1
            else:
                r_degraded += 1

        return {
            "nodes": {
                "total": total_nodes,
                "connected": connected_nodes,
                "streaming": streaming_nodes,
                "unreachable": unreachable_nodes,
            },
            "bgp": {
                "total": bgp_total,
                "established": bgp_established,
                "down": bgp_down,
            },
            "interfaces": {
                "total": itf_total,
                "down": itf_down,
                "errors": itf_errors,
            },
            "bridge_domains": {
                "total": len(bd_map),
                "up": bd_up,
                "degraded": bd_degraded,
                "down": bd_down,
                "instances": len(bd_instances_list),
            },
            "routers": {
                "total": len(router_map),
                "up": r_up,
                "degraded": r_degraded,
                "down": r_down,
                "instances": len(router_instances_list),
            },
            "telemetry": {
                "subscriptions": sum(len(s.status()["paths"]) for s in self._streams.values()),
                "resync_interval": self.resync_interval,
                "cached_tables": len(self._table_cache),
            },
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
