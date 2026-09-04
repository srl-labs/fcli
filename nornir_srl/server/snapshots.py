"""Saved renderings of a report, to compare a fabric against later.

A snapshot is one table written to a JSON file, with enough about how it was
taken to know what it can honestly be compared with: which report, which
fabric, which inventory filter, which parameters, and when. Comparing a table
against one taken of a different fabric, or under a different filter, would
read as half the nodes disappearing and another half arriving, so that is
refused rather than shown.

Explicit rather than continuous: a snapshot exists because someone decided the
fabric was worth remembering as it was. Nothing is recorded in the background,
so nothing grows without being asked to.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

#: Snapshots kept per report before the oldest is dropped. High enough that a
#: day of troubleshooting fits, low enough that a directory stays readable.
MAX_PER_REPORT = 20

#: What a label may be called on disk. Anything else in a label is replaced, so
#: a label can never reach outside the snapshot directory.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def default_directory() -> Path:
    """Where snapshots live unless the server was told otherwise."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(state) / "fcli" / "snapshots"


def _slug(text: str, fallback: str = "snapshot") -> str:
    # Leading dots and dashes are stripped as well as replaced: a name that
    # slugs to '..' or '-rf' is a filename nobody wants to have written.
    cleaned = _UNSAFE.sub("-", str(text or "")).strip("-.")
    return cleaned[:60].strip("-.") or fallback


@dataclass(frozen=True)
class Snapshot:
    """One saved table, and what it was taken of."""

    id: str
    report: str
    label: str
    taken_at: float
    inv_filter: Dict[str, str]
    params: Dict[str, Any]
    #: Nodes the table covered when it was taken.
    nodes: List[str]
    table: Dict[str, Any]
    #: The fabric this was taken of, when it has a name: the containerlab
    #: topology. Empty for an inventory that came from a Nornir config file,
    #: which has no name of its own.
    fabric: str = ""
    #: Every node the inventory held, which the row-derived ``nodes`` does not
    #: give: a report has no rows for a node with nothing to report.
    inventory: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "report": self.report,
            "label": self.label,
            "taken_at": self.taken_at,
            "inv_filter": self.inv_filter,
            "params": self.params,
            "nodes": self.nodes,
            "fabric": self.fabric,
            "inventory": self.inventory,
            "rows": len(self.table.get("rows") or []),
        }


class SnapshotStore:
    """The snapshots on disk, as a directory of JSON files."""

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = Path(directory) if directory else default_directory()

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #

    def list(self, report: Optional[str] = None) -> List[Snapshot]:
        """Every snapshot, newest first, optionally of one report only."""
        found: List[Snapshot] = []
        if not self.directory.is_dir():
            return found
        for path in self.directory.glob("*.json"):
            snapshot = self._read(path)
            if snapshot is None:
                continue
            if report and snapshot.report != report:
                continue
            found.append(snapshot)
        found.sort(key=lambda s: s.taken_at, reverse=True)
        return found

    def get(self, snapshot_id: str) -> Optional[Snapshot]:
        path = self._path(snapshot_id)
        return self._read(path) if path.is_file() else None

    def _read(self, path: Path) -> Optional[Snapshot]:
        try:
            with path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
            return Snapshot(
                id=str(raw["id"]),
                report=str(raw["report"]),
                label=str(raw.get("label") or ""),
                taken_at=float(raw.get("taken_at") or 0.0),
                inv_filter=dict(raw.get("inv_filter") or {}),
                params=dict(raw.get("params") or {}),
                nodes=list(raw.get("nodes") or []),
                table=dict(raw.get("table") or {}),
                # Absent from snapshots written before a fabric was recorded.
                # Those stay readable, and stay comparable: an unknown fabric
                # is not evidence of a different one.
                fabric=str(raw.get("fabric") or ""),
                inventory=list(raw.get("inventory") or []),
            )
        except (OSError, ValueError, KeyError) as exc:
            # A file written by a newer version, or half-written by a crash.
            # One unreadable snapshot must not hide the rest.
            logger.warning("ignoring unreadable snapshot %s: %s", path.name, exc)
            return None

    # ------------------------------------------------------------------ #
    # writing
    # ------------------------------------------------------------------ #

    def save(
        self,
        report: str,
        table: Mapping[str, Any],
        *,
        label: str = "",
        inv_filter: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        nodes: Optional[List[str]] = None,
        fabric: str = "",
        inventory: Optional[List[str]] = None,
    ) -> Snapshot:
        taken_at = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(taken_at))
        snapshot = Snapshot(
            id=f"{_slug(report)}-{stamp}-{uuid.uuid4().hex[:6]}",
            report=report,
            label=label.strip() or stamp,
            taken_at=taken_at,
            inv_filter=dict(inv_filter or {}),
            params=dict(params or {}),
            nodes=list(nodes or _nodes_of(table)),
            table=dict(table),
            fabric=str(fabric or ""),
            inventory=sorted(inventory or []),
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(snapshot.id)
        # Written beside and moved into place, so a reader never sees half a
        # file and a crash mid-write leaves the previous snapshots intact.
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "id": snapshot.id,
                    "report": snapshot.report,
                    "label": snapshot.label,
                    "taken_at": snapshot.taken_at,
                    "inv_filter": snapshot.inv_filter,
                    "params": snapshot.params,
                    "nodes": snapshot.nodes,
                    "fabric": snapshot.fabric,
                    "inventory": snapshot.inventory,
                    "table": snapshot.table,
                },
                handle,
                default=str,
            )
        temporary.replace(path)
        self._prune(report)
        logger.info(
            "saved snapshot %s of report '%s' (%d row(s))",
            snapshot.id,
            report,
            len(snapshot.table.get("rows") or []),
        )
        return snapshot

    def delete(self, snapshot_id: str) -> bool:
        path = self._path(snapshot_id)
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def _prune(self, report: str) -> None:
        for stale in self.list(report)[MAX_PER_REPORT:]:
            self.delete(stale.id)

    def _path(self, snapshot_id: str) -> Path:
        # The id is slugged on the way in and on the way out, so an id from a
        # query string cannot name a file outside the directory.
        return self.directory / f"{_slug(snapshot_id, 'unknown')}.json"


def _nodes_of(table: Mapping[str, Any]) -> List[str]:
    return sorted({str(row.get("Node", "")) for row in table.get("rows") or []} - {""})


def comparable(
    snapshot: Snapshot,
    inv_filter: Optional[Mapping[str, str]],
    params: Optional[Mapping[str, Any]],
    fabric: str = "",
    inventory: Optional[List[str]] = None,
) -> Optional[str]:
    """Why *snapshot* cannot be compared with a table taken like this, if it cannot.

    A table rendered against a different fabric, or under a different inventory
    filter, covers different nodes, so every node the two do not share would
    read as one that appeared or went away. That is not a diff worth showing,
    and it is worth saying why.

    Both fabric tests are deliberately loose, because the honest failure to
    catch is *another fabric*, not a fabric that changed. A node added or
    decommissioned since the snapshot is exactly what a comparison is for.
    """
    if snapshot.fabric and fabric and snapshot.fabric != fabric:
        return (
            f"the snapshot was taken of fabric '{snapshot.fabric}', "
            f"and this table of '{fabric}'"
        )
    # Nothing in common is not a fabric that changed, it is a different one.
    # An empty list on either side means nobody recorded an inventory - an old
    # snapshot, or a surface that does not know - so there is nothing to judge.
    before, now = set(snapshot.inventory), set(inventory or [])
    if before and now and not (before & now):
        return (
            f"the snapshot was taken of {_names(before)}, and this table of "
            f"{_names(now)}, which share no node"
        )
    if dict(snapshot.inv_filter) != dict(inv_filter or {}):
        return (
            f"the snapshot was taken with inventory filter "
            f"{snapshot.inv_filter or 'none'}, and this table with "
            f"{dict(inv_filter or {}) or 'none'}"
        )
    if dict(snapshot.params) != dict(params or {}):
        return (
            f"the snapshot was taken with parameters {snapshot.params or 'none'}, "
            f"and this table with {dict(params or {}) or 'none'}"
        )
    return None


def _names(nodes: set) -> str:
    """A node set as something short enough to put in an error message."""
    listed = sorted(nodes)
    shown = ", ".join(listed[:3])
    return shown if len(listed) <= 3 else f"{shown} and {len(listed) - 3} more"
