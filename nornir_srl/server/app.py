"""Starlette application serving the fcli live report tables."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

import anyio
from nornir.core import Nornir
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..diff import diff_nodes, diff_tables
from ..reports import SERVER, ReportSpec, coerce_params, get_report, reports_for
from .agent import NO_PROVIDER, ChatService
from .snapshots import SnapshotStore, comparable
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

#: Placeholder in index.html for the cache key of the assets it pulls in.
ASSET_TOKEN = "__ASSETS__"


def asset_version() -> str:
    """A token that changes whenever the bundled JS or CSS does.

    The page itself is served ``no-cache`` so the browser revalidates it on
    every load, and the assets it references are fingerprinted with this. Left
    to itself a browser will happily keep serving a page it cached before a
    feature existed, which shows up as a report whose panel is simply missing:
    the report list comes from the API and lists it, the markup it needs does
    not exist, and nothing in the console says why.
    """
    newest = max(
        (STATIC_DIR / name).stat().st_mtime_ns for name in ("app.js", "style.css")
    )
    return f"{__version__}-{newest:x}"


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
    params: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[bytes]:
    """Yield server-sent events for *report* until the client goes away.

    A table is only pushed when it actually changed, so an idle fabric costs
    nothing but the periodic keep-alive comment.
    """
    loop = asyncio.get_running_loop()
    last_digest = ""
    last_sent = 0.0
    try:
        while not await is_disconnected() and not store.stopping:
            try:
                table = await anyio.to_thread.run_sync(
                    store.table, report, inv_filter, params
                )
            except (asyncio.CancelledError, GeneratorExit):
                break
            except Exception as exc:  # noqa: BLE001 - surfaced in the browser
                if store.stopping:
                    return
                logger.exception("rendering report '%s' failed", report.name)
                payload = json.dumps({"error": str(exc)})
                yield f"event: error\ndata: {payload}\n\n".encode()
                await asyncio.sleep(interval)
                continue
            if store.stopping:
                return
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
            deadline = loop.time() + interval
            while loop.time() < deadline:
                if store.stopping or await is_disconnected():
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.sleep(min(0.1, remaining))
                except (asyncio.CancelledError, GeneratorExit):
                    return
    except (asyncio.CancelledError, GeneratorExit):
        return


class SuppressCancelledErrorMiddleware:
    """Catch asyncio.CancelledError during uvicorn shutdown to prevent traceback noise."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.store = getattr(getattr(app, "state", None), "store", None)

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
    snapshot_dir: Optional[Path] = None,
    chat_client_factory: Optional[Callable[[], Any]] = None,
    jsonrpc_call: Optional[Callable[..., Any]] = None,
) -> Starlette:
    """Build the fcli server application around an initialized Nornir inventory."""
    snapshot_store = SnapshotStore(snapshot_dir)
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
    chat_kwargs: Dict[str, Any] = {}
    if chat_client_factory is not None:
        chat_kwargs["client_factory"] = chat_client_factory
    if jsonrpc_call is not None:
        chat_kwargs["jsonrpc_call"] = jsonrpc_call
    chat = ChatService(store, **chat_kwargs)
    chat_gate = asyncio.Semaphore(1)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await anyio.to_thread.run_sync(store.start)
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(store.stop)

    async def index(_request: Request) -> Response:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace(ASSET_TOKEN, asset_version()),
            headers={"Cache-Control": "no-cache"},
        )

    async def reports(_request: Request) -> Response:
        return JSONResponse(
            {
                "version": __version__,
                "topo_name": store.topo_name,
                "reports": [r.as_dict() for r in reports_for(SERVER)],
                "chat": {
                    "enabled": chat.enabled(),
                    "providers": chat.providers(),
                },
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

    async def topology(request: Request) -> Response:
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        return JSONResponse(await anyio.to_thread.run_sync(store.topology, inv_filter))

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
        try:
            params = coerce_params(report, request.query_params)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        table = await anyio.to_thread.run_sync(store.table, report, inv_filter, params)
        return JSONResponse(table)

    async def report_stream(request: Request) -> Response:
        try:
            report = streamable_report(request.path_params["name"])
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        try:
            params = coerce_params(report, request.query_params)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            interval = max(float(request.query_params.get("refresh", refresh)), 0.5)
        except ValueError:
            interval = refresh

        return StreamingResponse(
            table_events(
                store,
                report,
                inv_filter,
                interval,
                request.is_disconnected,
                params,
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # ------------------------------------------------------------------ #
    # snapshots and comparison
    # ------------------------------------------------------------------ #

    async def snapshots(request: Request) -> Response:
        report = request.query_params.get("report") or None
        saved = await anyio.to_thread.run_sync(snapshot_store.list, report)
        return JSONResponse({"snapshots": [s.as_dict() for s in saved]})

    async def snapshot_save(request: Request) -> Response:
        """Render *name* now, and keep it to compare against later."""
        try:
            report = streamable_report(request.path_params["name"])
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        try:
            params = coerce_params(report, request.query_params)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        label = request.query_params.get("label", "")
        table = await anyio.to_thread.run_sync(store.table, report, inv_filter, params)
        saved = await anyio.to_thread.run_sync(
            functools.partial(
                snapshot_store.save,
                report.name,
                table,
                label=label,
                inv_filter=inv_filter,
                params=params,
                fabric=store.topo_name or "",
                inventory=store.targets(inv_filter),
            )
        )
        return JSONResponse(saved.as_dict(), status_code=201)

    async def snapshot_delete(request: Request) -> Response:
        removed = await anyio.to_thread.run_sync(
            snapshot_store.delete, request.path_params["snapshot_id"]
        )
        if not removed:
            return JSONResponse({"error": "no such snapshot"}, status_code=404)
        return JSONResponse({"deleted": request.path_params["snapshot_id"]})

    async def report_diff(request: Request) -> Response:
        """Compare a report against a snapshot of it, or one node against another."""
        try:
            report = streamable_report(request.path_params["name"])
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        try:
            params = coerce_params(report, request.query_params)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        include_same = request.query_params.get("same") in ("1", "true", "yes")

        against = request.query_params.get("against")
        nodes = request.query_params.get("nodes")
        if bool(against) == bool(nodes):
            return JSONResponse(
                {"error": "give either 'against=<snapshot id>' or 'nodes=<a>,<b>'"},
                status_code=400,
            )

        table = await anyio.to_thread.run_sync(store.table, report, inv_filter, params)

        if nodes:
            wanted = [n.strip() for n in nodes.split(",") if n.strip()]
            if len(wanted) != 2:
                return JSONResponse(
                    {"error": "'nodes' takes exactly two node names"}, status_code=400
                )
            return JSONResponse(
                diff_nodes(
                    table,
                    wanted[0],
                    wanted[1],
                    report.key_columns,
                    include_same=include_same,
                )
            )

        snapshot = await anyio.to_thread.run_sync(snapshot_store.get, str(against))
        if snapshot is None:
            return JSONResponse({"error": "no such snapshot"}, status_code=404)
        if snapshot.report != report.name:
            return JSONResponse(
                {"error": f"that snapshot is of report '{snapshot.report}'"},
                status_code=400,
            )
        mismatch = comparable(
            snapshot,
            inv_filter,
            params,
            fabric=store.topo_name or "",
            inventory=store.targets(inv_filter),
        )
        if mismatch:
            return JSONResponse({"error": mismatch}, status_code=409)
        return JSONResponse(
            diff_tables(
                snapshot.table,
                table,
                report.key_columns,
                labels=(snapshot.label, "now"),
                include_same=include_same,
            )
        )

    async def chat_turn(request: Request) -> Response:
        if not chat.enabled():
            return JSONResponse({"error": NO_PROVIDER}, status_code=503)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - bad client body
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON object expected"}, status_code=400)
        messages = body.get("messages")
        if not isinstance(messages, list):
            return JSONResponse({"error": "messages must be a list"}, status_code=400)
        context = body.get("context")
        if context is not None and not isinstance(context, dict):
            return JSONResponse({"error": "context must be an object"}, status_code=400)
        provider = body.get("provider")
        if provider is not None and not isinstance(provider, str):
            return JSONResponse({"error": "provider must be a string"}, status_code=400)
        effort = body.get("effort")
        if effort is not None and not isinstance(effort, str):
            return JSONResponse({"error": "effort must be a string"}, status_code=400)
        try:
            await asyncio.wait_for(chat_gate.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": "another chat is in progress"}, status_code=429
            )

        async def gated() -> AsyncIterator[bytes]:
            try:
                async for chunk in chat.events(messages, context, provider, effort):
                    yield chunk
            finally:
                chat_gate.release()

        return StreamingResponse(
            gated(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    routes = [
        Route("/", index),
        Route("/api/reports", reports),
        Route("/api/inventory", inventory),
        Route("/api/status", status),
        Route("/api/overview", overview),
        Route("/api/topology", topology),
        Route("/api/report/{name}", report_once),
        Route("/api/stream/{name}", report_stream),
        Route("/api/diff/{name}", report_diff),
        Route("/api/snapshots", snapshots),
        Route("/api/snapshot/{name}", snapshot_save, methods=["POST"]),
        Route("/api/snapshot/{snapshot_id}", snapshot_delete, methods=["DELETE"]),
        Route("/api/chat", chat_turn, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.store = store
    app.state.chat = chat
    wrapped = SuppressCancelledErrorMiddleware(app)
    wrapped.store = store
    return wrapped


class _QuietUvicornShutdown(logging.Filter):
    """Drop uvicorn's timeout-on-Ctrl+C ERROR; we tear gNMI down ourselves."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "timeout graceful shutdown exceeded" not in record.getMessage()


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
    snapshot_dir: Optional[Path] = None,
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
        snapshot_dir=snapshot_dir,
    )
    store = app.store
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=False,
        timeout_graceful_shutdown=5.0,
    )
    server = uvicorn.Server(config)
    # Tear the store down as soon as SIGINT/SIGTERM arrives, not after uvicorn
    # has already waited out timeout_graceful_shutdown. SSE streams sit in
    # in-flight Gets; closing gNMI first lets those tasks finish so uvicorn
    # never logs "Cancel N running task(s), timeout graceful shutdown exceeded".
    original_exit = server.handle_exit

    def handle_exit(sig: int, frame: Any) -> None:
        threading.Thread(target=store.stop, name="fcli-shutdown", daemon=True).start()
        original_exit(sig, frame)

    server.handle_exit = handle_exit  # type: ignore[method-assign]
    logging.getLogger("uvicorn.error").addFilter(_QuietUvicornShutdown())
    try:
        server.run()
    except (KeyboardInterrupt, SystemExit):
        pass
