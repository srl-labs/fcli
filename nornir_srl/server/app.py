"""Starlette application serving the fcli live report tables."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

import anyio
from nornir.core import Nornir
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..reports import SERVER, ReportSpec, get_report, reports_for
from .store import FabricStore

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: How often the browser is offered a fresh render of the table.
DEFAULT_REFRESH = 2.0
#: Keep-alive comment interval so proxies do not drop an idle SSE stream.
SSE_HEARTBEAT = 20.0

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def parse_kv(values: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse a ``k=v,k=v`` query argument into a dict."""
    if not values:
        return None
    parsed: Dict[str, str] = {}
    for part in values.split(","):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            parsed[key.strip()] = value.strip()
    return parsed or None


def table_digest(table: Dict[str, Any]) -> str:
    """Fingerprint the parts of a rendered table the browser actually shows."""
    material = json.dumps(
        {"c": table.get("columns"), "r": table.get("rows"), "e": table.get("errors")},
        default=str,
        sort_keys=True,
    )
    return hashlib.sha1(material.encode()).hexdigest()


async def table_events(
    store: FabricStore,
    report: ReportSpec,
    inv_filter: Optional[Dict[str, str]],
    interval: float,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[bytes]:
    """Yield server-sent events for *report* until the client goes away.

    A table is only pushed when it actually changed, so an idle fabric costs
    nothing but the periodic keep-alive comment.
    """
    loop = asyncio.get_running_loop()
    last_digest = ""
    last_sent = 0.0
    try:
        while not await is_disconnected():
            try:
                table = await anyio.to_thread.run_sync(store.table, report, inv_filter)
            except (asyncio.CancelledError, GeneratorExit):
                break
            except Exception as exc:  # noqa: BLE001 - surfaced in the browser
                logger.exception("rendering report '%s' failed", report.name)
                payload = json.dumps({"error": str(exc)})
                yield f"event: error\ndata: {payload}\n\n".encode()
                await asyncio.sleep(interval)
                continue
            digest = table_digest(table)
            now = loop.time()
            if digest != last_digest:
                last_digest = digest
                last_sent = now
                body = json.dumps(table, default=str)
                yield f"event: table\ndata: {body}\n\n".encode()
            elif now - last_sent > SSE_HEARTBEAT:
                last_sent = now
                yield b": keep-alive\n\n"
            try:
                await asyncio.sleep(interval)
            except (asyncio.CancelledError, GeneratorExit):
                break
    except (asyncio.CancelledError, GeneratorExit):
        return


class SuppressCancelledErrorMiddleware:
    """Catch asyncio.CancelledError during uvicorn shutdown to prevent traceback noise."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        except (asyncio.CancelledError, GeneratorExit):
            pass


def create_app(
    nornir: Nornir,
    *,
    sample_interval: Optional[int] = None,
    resync_interval: int = 300,
    refresh: float = DEFAULT_REFRESH,
    workers: int = 20,
    idle_timeout: float = 900.0,
    restart_debounce: float = 1.0,
    connect_retry_interval: float = 30.0,
    topo_name: Optional[str] = None,
) -> Starlette:
    """Build the fcli server application around an initialized Nornir inventory."""
    store = FabricStore(
        nornir,
        sample_interval=sample_interval,
        resync_interval=resync_interval,
        workers=workers,
        idle_timeout=idle_timeout,
        restart_debounce=restart_debounce,
        connect_retry_interval=connect_retry_interval,
        topo_name=topo_name,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await anyio.to_thread.run_sync(store.start)
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(store.stop)

    async def index(_request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html")

    async def reports(_request: Request) -> Response:
        return JSONResponse(
            {
                "version": __version__,
                "topo_name": store.topo_name,
                "reports": [r.as_dict() for r in reports_for(SERVER)],
            }
        )

    async def inventory(_request: Request) -> Response:
        hosts = await anyio.to_thread.run_sync(store.inventory)
        return JSONResponse({"hosts": hosts})

    async def status(_request: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(store.status))

    async def overview(request: Request) -> Response:
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        return JSONResponse(await anyio.to_thread.run_sync(store.overview, inv_filter))

    def streamable_report(name: str) -> ReportSpec:
        """The named report, provided the server is able to stream it."""
        report = get_report(name)
        if not report.on(SERVER):
            raise KeyError(f"report '{name}' cannot be streamed")
        return report

    async def report_once(request: Request) -> Response:
        try:
            report = streamable_report(request.path_params["name"])
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        table = await anyio.to_thread.run_sync(store.table, report, inv_filter)
        return JSONResponse(table)

    async def report_stream(request: Request) -> Response:
        try:
            report = streamable_report(request.path_params["name"])
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        try:
            interval = max(float(request.query_params.get("refresh", refresh)), 0.5)
        except ValueError:
            interval = refresh

        return StreamingResponse(
            table_events(store, report, inv_filter, interval, request.is_disconnected),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    routes = [
        Route("/", index),
        Route("/api/reports", reports),
        Route("/api/inventory", inventory),
        Route("/api/status", status),
        Route("/api/overview", overview),
        Route("/api/report/{name}", report_once),
        Route("/api/stream/{name}", report_stream),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.store = store
    return SuppressCancelledErrorMiddleware(app)


def serve(
    nornir: Nornir,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    sample_interval: Optional[int] = None,
    resync_interval: int = 300,
    refresh: float = DEFAULT_REFRESH,
    workers: int = 20,
    idle_timeout: float = 900.0,
    log_level: str = "info",
    topo_name: Optional[str] = None,
) -> None:
    """Run the fcli server with uvicorn (blocking)."""
    import uvicorn

    app = create_app(
        nornir,
        sample_interval=sample_interval,
        resync_interval=resync_interval,
        refresh=refresh,
        workers=workers,
        idle_timeout=idle_timeout,
        topo_name=topo_name,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=False,
        timeout_graceful_shutdown=5.0,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except (KeyboardInterrupt, SystemExit):
        pass
