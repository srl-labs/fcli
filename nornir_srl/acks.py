"""Acknowledged findings: what someone has seen, and does not need shown again.

An incident that is known - a link waiting for a technician, BFD that will be
enabled on the spines in the next change window - keeps drawing the eye on
every page until it is fixed. Acknowledging it takes it out of the counts and
colours that exist to say *look here*, without hiding that it is still there.

An acknowledgement is recorded per *finding* rather than per incident, which
keeps it honest as the fabric changes underneath it:

* an incident is acknowledged while every finding it holds is; a new finding
  joining it - a second session going down over the same link - brings it
  back, because that is something nobody has seen yet;
* an acknowledgement ends when its finding does, so the same fault coming
  back later is an alarm again rather than silently pre-acknowledged.

The store is shared by everyone looking at one server, and kept on disk per
fabric, so a restart does not bring back everything that was acknowledged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: What identifies a finding across readings: its check, node and subject.
Key = Tuple[str, str, str]


def finding_key(finding: Any) -> Key:
    return (finding.check, finding.node, finding.subject)


@dataclass(frozen=True)
class Ack:
    """One acknowledged finding."""

    check: str
    node: str
    subject: str
    #: When, as a Unix timestamp.
    at: float
    #: What the person acknowledging it wrote, if anything.
    note: str = ""
    #: The incident it was acknowledged as part of, as titled then.
    incident: str = ""
    #: And that incident's id, which is what takes the acknowledgement off
    #: again even once the incident has changed shape.
    incident_id: str = ""

    @property
    def key(self) -> Key:
        return (self.check, self.node, self.subject)


class AckStore:
    """The acknowledged findings of one fabric, optionally kept in *path*."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._acks: Dict[Key, Ack] = {}
        if path is not None:
            self._load(path)

    def acknowledge(
        self,
        findings: Iterable[Any],
        note: str = "",
        incident: str = "",
        at: Optional[float] = None,
        incident_id: str = "",
    ) -> List[Ack]:
        """Acknowledge *findings*; returns the acknowledgements made."""
        at = time.time() if at is None else at
        made = [
            Ack(f.check, f.node, f.subject, at, note.strip(), incident, incident_id)
            for f in findings
        ]
        with self._lock:
            for ack in made:
                self._acks[ack.key] = ack
        self._save()
        return made

    def unacknowledge(self, keys: Iterable[Key]) -> List[Ack]:
        """Take the acknowledgement off *keys*; returns the ones there were."""
        with self._lock:
            dropped = [self._acks.pop(tuple(key)) for key in keys if tuple(key) in self._acks]
        if dropped:
            self._save()
        return dropped

    def expire(self, gone: Iterable[Key]) -> List[Ack]:
        """End the acknowledgements of findings that are no longer there."""
        return self.unacknowledge(gone)

    def of_incident(self, incident_id: str) -> Set[Key]:
        """The findings acknowledged as part of the incident *incident_id*."""
        with self._lock:
            return {key for key, ack in self._acks.items() if ack.incident_id == incident_id}

    def keys(self) -> Set[Key]:
        with self._lock:
            return set(self._acks)

    def get(self, key: Key) -> Optional[Ack]:
        with self._lock:
            return self._acks.get(key)

    def all(self) -> List[Ack]:
        with self._lock:
            return sorted(self._acks.values(), key=lambda a: (-a.at, a.node, a.check, a.subject))

    # -- persistence ----------------------------------------------------------

    def _load(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in raw if isinstance(raw, list) else ():
            try:
                ack = Ack(
                    str(item["check"]),
                    str(item["node"]),
                    str(item["subject"]),
                    float(item["at"]),
                    str(item.get("note", "")),
                    str(item.get("incident", "")),
                    str(item.get("incident_id", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._acks[ack.key] = ack

    def _save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = [asdict(ack) for ack in self._acks.values()]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("could not keep the acknowledgements in %s: %s", self.path, exc)


def is_acknowledged(incident: Any, acked: Set[Key]) -> bool:
    """Whether every finding of *incident* is acknowledged."""
    return bool(acked) and all(finding_key(f) in acked for f in incident.findings)


def mark(incidents: Iterable[Any], acked: Set[Key]) -> List[Any]:
    """*incidents* with their ``acknowledged`` flag set, the open ones first."""
    marked = [replace(i, acknowledged=is_acknowledged(i, acked)) for i in incidents]
    # A stable sort: within each half, the order the correlation gave.
    marked.sort(key=lambda i: i.acknowledged)
    return marked


__all__ = ["Ack", "AckStore", "Key", "finding_key", "is_acknowledged", "mark"]
