"""Read-only MD-CLI allowlist for the in-server troubleshooting agent.

JSON-RPC ``cli`` can run anything the node accepts, including ``set`` / ``commit``.
The agent is read-only, so every command is checked here before it leaves the
server. gNMI origin ``cli`` is a Set/commit path on SR Linux and is never used.
"""

from __future__ import annotations

import re

ALLOWED_FIRST = frozenset({"show", "info", "tree"})

#: Redirects, chaining, expansion — one statement only.
_UNSAFE = re.compile(r"[|;`$]|&&|\n")


class CliRejected(ValueError):
    """The command is not a read-only show/info/tree statement."""


def check_cli(command: str) -> str:
    """Return a stripped command, or raise :class:`CliRejected`."""
    cmd = (command or "").strip()
    if not cmd:
        raise CliRejected("empty command")
    if _UNSAFE.search(cmd):
        raise CliRejected(
            "command must be a single show/info/tree statement "
            "(no pipes, chaining, or newlines)"
        )
    first = cmd.split()[0].lower()
    if first not in ALLOWED_FIRST:
        raise CliRejected(
            f"only show, info, and tree are allowed; not '{cmd.split()[0]}'"
        )
    return cmd


def check_gnmi_path(path: str) -> str:
    """A YANG path for a read-only Get; CLI origin is not allowed."""
    p = (path or "").strip()
    if not p.startswith("/"):
        raise CliRejected("gNMI path must start with /")
    lower = p.lower()
    if lower.startswith("cli:") or lower.startswith("/cli") or "origin=cli" in lower:
        raise CliRejected("CLI origin is not allowed on Get")
    return p
