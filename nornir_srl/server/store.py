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
    ) -> None:
        self.nornir = nornir
        self.sample_interval = sample_interval
        self.resync_interval = resync_interval
        self._pool = ThreadPoolExecutor(
            max_workers=max(workers, 1), thread_name_prefix="fcli-srv"
        )
        self._streams: Dict[str, HostStream] = {}
        self._connect_errors: Dict[str, str] = {}
        self._activated: Dict[Tuple[str, str], bool] = {}
        self._activation_errors: Dict[Tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._resync_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Open a gNMI connection to every node in the inventory."""
        hosts = list(self.nornir.inventory.hosts.items())
        list(self._pool.map(lambda item: self._connect(*item), hosts))
        connected = len(self._streams)
        logger.info("connected to %d/%d node(s)", connected, len(hosts))
        if self.resync_interval > 0:
            self._resync_thread = threading.Thread(
                target=self._resync_loop, name="fcli-resync", daemon=True
            )
            self._resync_thread.start()

    def _connect(self, name: str, host: Any) -> None:
        try:
            device = host.get_connection(CONNECTION_NAME, self.nornir.config)
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.warning("%s: connection failed: %s", name, exc)
            with self._lock:
                self._connect_errors[name] = str(exc)
            return
        with self._lock:
            self._connect_errors.pop(name, None)
            self._streams[name] = HostStream(
                name,
                device,
                default_sample_interval=self.sample_interval or 15,
            )

    def _resync_loop(self) -> None:
        while not self._stop.wait(self.resync_interval):
            for stream in list(self._streams.values()):
                try:
                    stream.resync()
                except Exception as exc:  # noqa: BLE001 - best effort
                    logger.debug("%s: resync failed: %s", stream.name, exc)

    def stop(self) -> None:
        self._stop.set()
        for stream in list(self._streams.values()):
            stream.stop()
        self._pool.shutdown(wait=False)

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
                    # 'connected' is about the gNMI session; 'streaming' says
                    # whether a Subscribe RPC is currently running on it, which
                    # only happens once a report has been opened.
                    "connected": stream is not None,
                    "streaming": bool(stream and stream.connected),
                    "error": self._connect_errors.get(name)
                    or (stream.error if stream else None),
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
        """Make sure every node streams the paths *report* needs."""
        names = hosts if hosts is not None else list(self._streams)
        pending = []
        with self._lock:
            for name in names:
                if name not in self._streams:
                    continue
                if self._activated.get((name, report.name)):
                    continue
                pending.append(name)
        if not pending:
            return
        list(self._pool.map(lambda n: self._activate_host(report, n), pending))

    def _activate_host(self, report: Report, name: str) -> None:
        stream = self._streams.get(name)
        if stream is None:
            return
        key = (name, report.name)
        try:
            specs = self._discover(report, stream)
            stream.ensure_paths(specs)
        except Exception as exc:  # noqa: BLE001 - reported per node in the UI
            logger.warning(
                "%s: activating report '%s' failed: %s", name, report.name, exc
            )
            with self._lock:
                self._activation_errors[key] = str(exc)
                # Mark as done so a device that does not support the report is
                # not re-probed on every refresh.
                self._activated[key] = True
            return
        with self._lock:
            self._activation_errors.pop(key, None)
            self._activated[key] = True

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
        recorder = RecordingDevice(stream.device)
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
        started = time.time()
        names = self._targets(inv_filter)
        self.activate(report, names)

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
        return {
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

    def _host_rows(
        self, report: Report, name: str
    ) -> Tuple[str, List[str], List[Dict[str, Any]], Optional[str]]:
        stream = self._streams.get(name)
        if stream is None:
            return name, [], [], self._connect_errors.get(name, "not connected")
        activation_error = self._activation_errors.get((name, report.name))
        if activation_error:
            return name, [], [], activation_error
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
            "resync_interval": self.resync_interval,
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
