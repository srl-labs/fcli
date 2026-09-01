"""HTTP JSON-RPC client for SR Linux ``cli`` (show/info), not gNMI.

Containerlab enables JSON-RPC on the mgmt instance (HTTP ``/jsonrpc``). Hardware
fabrics may not; callers should surface that clearly so the agent can fall back
to a report or a YANG Get.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_JSONRPC_PORT = 80
DEFAULT_TIMEOUT = 15.0


class JsonRpcUnreachable(RuntimeError):
    """The node's HTTP JSON-RPC interface did not answer at all.

    Distinct from a command the node rejected: this means the interface is off
    or firewalled, so every other node in the fabric is likely the same.
    """


def jsonrpc_cli(
    host: str,
    port: int,
    username: str,
    password: str,
    command: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """Run one MD-CLI command via JSON-RPC ``cli`` and return the result payload."""
    url = f"http://{host}:{port}/jsonrpc"
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "cli",
            "params": {"commands": [command]},
        }
    ).encode()
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except urllib.error.URLError as exc:
        raise JsonRpcUnreachable(
            f"JSON-RPC at {url} failed ({exc}). JSON-RPC is a separate HTTP "
            "interface from gNMI and must be enabled on the node (containerlab "
            "does this on mgmt). Use a report tool or node_get instead."
        ) from exc
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(str(body["error"]))
    if isinstance(body, dict):
        return body.get("result", body)
    return body
