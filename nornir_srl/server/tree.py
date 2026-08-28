"""In-memory YANG/gNMI tree used to hold streamed telemetry state.

A gNMI ``SubscribeResponse`` delivers updates as ``(path, value)`` pairs where
*path* addresses a single leaf, a container or a whole subtree. To be able to
re-use the report getters in :mod:`nornir_srl.connections` unchanged, the
streamed updates are merged into a tree that can be materialized back into the
exact same JSON structure a gNMI ``Get`` would have returned.

The only structural difference between a gNMI path and plain JSON is the YANG
list: ``interface[name=ethernet-1/1]`` addresses one entry of the ``interface``
list. Those are held in :class:`ListNode` so that individual entries can be
updated and deleted, and are rendered back as a list of dicts (with the key
leaves merged in) by :func:`materialize`.
"""

from __future__ import annotations

import copy
import fnmatch
import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "ListNode",
    "split_path",
    "parse_elem",
    "parse_path",
    "join_path",
    "insert",
    "delete",
    "get_node",
    "key_matches",
    "materialize",
    "select_path",
    "strip_module",
    "strip_values",
]

_MODULE_RE = re.compile(r"srl_nokia-[^:]+:")
_KEY_RE = re.compile(r"\[([^=\]]+)=([^\]]*)\]")


def strip_module(name: str) -> str:
    """Drop the YANG module prefix from a path element or value."""
    if name.startswith("srl_nokia-") and ":" in name:
        return _MODULE_RE.sub("", name)
    return name


class ListNode:
    """A YANG list: an ordered mapping of key-tuple -> (keys, child node)."""

    __slots__ = ("entries",)

    def __init__(self) -> None:
        # Key values keep the type the device reported them with where it is
        # known: a Get payload types them, a gNMI path can only spell them out.
        # Identity goes through _key_ident, so the two forms still collide.
        self.entries: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}

    def entry(self, keys: Dict[str, Any]) -> Dict[str, Any]:
        """Return (creating if needed) the child node for *keys*."""
        ident = _key_ident(keys)
        found = self.entries.get(ident)
        if found is None:
            child: Dict[str, Any] = {}
            self.entries[ident] = (dict(keys), child)
            return child
        return found[1]

    def put(self, ident: str, keys: Dict[str, Any], child: Dict[str, Any]) -> None:
        """Store *child* under an explicit identity (used when re-keying)."""
        self.entries[ident] = (dict(keys), child)

    def pop(self, keys: Dict[str, Any]) -> None:
        self.entries.pop(_key_ident(keys), None)

    def __len__(self) -> int:
        return len(self.entries)


def _key_ident(keys: Dict[str, Any]) -> str:
    return ",".join(f"{k}={keys[k]}" for k in sorted(keys))


def split_path(path: str) -> List[str]:
    """Split a gNMI path on ``/``, ignoring separators inside ``[...]`` keys.

    SR Linux key values routinely contain slashes (``interface[name=ethernet-1/1]``)
    so a plain ``str.split`` is not usable here.
    """
    elems: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in path:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "/" and depth == 0:
            elems.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    elems.append("".join(buf))
    return [e for e in elems if e]


def parse_elem(elem: str) -> Tuple[str, Dict[str, str]]:
    """Split ``name[k1=v1][k2=v2]`` into ``("name", {"k1": "v1", "k2": "v2"})``."""
    bracket = elem.find("[")
    if bracket == -1:
        return strip_module(elem), {}
    name = strip_module(elem[:bracket])
    keys = {k: v for k, v in _KEY_RE.findall(elem[bracket:])}
    return name, keys


def parse_path(path: str) -> List[Tuple[str, Dict[str, str]]]:
    """Parse a gNMI path string into a list of ``(name, keys)`` elements."""
    return [parse_elem(e) for e in split_path(path)]


def join_path(*parts: Optional[str]) -> str:
    """Join gNMI path fragments, dropping empty ones and duplicate slashes."""
    joined = "/".join(p.strip("/") for p in parts if p)
    return joined


def _as_list_node(parent: Dict[str, Any], name: str, key_names: List[str]) -> ListNode:
    """Return the :class:`ListNode` at ``parent[name]``, converting if needed."""
    current = parent.get(name)
    if isinstance(current, ListNode):
        return current
    node = ListNode()
    if isinstance(current, list):
        # A previous bulk (subtree) update stored an opaque list of dicts. Now
        # that the key leaf names are known, promote it to a list node so
        # per-entry updates and deletes land in the right entry.
        for index, item in enumerate(current):
            if not isinstance(item, dict):
                continue
            if key_names and all(k in item for k in key_names):
                item_keys = {k: item[k] for k in key_names}
                entry = node.entry(item_keys)
                entry.update({k: v for k, v in item.items() if k not in item_keys})
            else:
                # Key leaves absent (the target did not report them): keep the
                # entry under a synthetic identity rather than dropping data.
                node.put(f"\x00unkeyed-{index}", {}, dict(item))
    elif isinstance(current, dict) and key_names:
        # A single (unkeyed) entry previously stored as a dict.
        if all(k in current for k in key_names):
            item_keys = {k: current[k] for k in key_names}
            entry = node.entry(item_keys)
            entry.update({k: v for k, v in current.items() if k not in item_keys})
    parent[name] = node
    return node


def _as_container(parent: Dict[str, Any], name: str) -> Dict[str, Any]:
    current = parent.get(name)
    if isinstance(current, dict):
        return current
    node: Dict[str, Any] = {}
    parent[name] = node
    return node


def _list_key_names(node: ListNode) -> List[str]:
    for keys, _child in node.entries.values():
        if keys:
            return list(keys)
    return []


def _descend(
    root: Dict[str, Any],
    elems: List[Tuple[str, Dict[str, str]]],
    create: bool,
) -> Optional[Dict[str, Any]]:
    """Walk *elems* from *root*, returning the container node they address."""
    node: Dict[str, Any] = root
    for name, keys in elems:
        if keys:
            if not create:
                current = node.get(name)
                if not isinstance(current, ListNode):
                    return None
                found = current.entries.get(_key_ident(keys))
                if found is None:
                    return None
                node = found[1]
            else:
                node = _as_list_node(node, name, list(keys)).entry(keys)
        else:
            if not create:
                current = node.get(name)
                if not isinstance(current, dict):
                    return None
                node = current
            else:
                node = _as_container(node, name)
    return node


def insert(
    root: Dict[str, Any],
    path: str,
    value: Any,
    key_hints: Optional[Dict[str, List[str]]] = None,
) -> None:
    """Merge *value* into *root* at *path*.

    A scalar replaces the leaf; a JSON object is merged so that a bulk subtree
    update followed by individual leaf updates converges on the same state.

    *key_hints* maps a YANG list name to its key leaves. gNMI ``Get`` responses
    return lists inline, without telling us which leaves are the keys, so the
    caller passes what it knows from the requested path. Without a hint a list
    is stored opaquely and is promoted later, when a keyed update names its
    keys.
    """
    value = strip_values(value)
    elems = parse_path(path)
    if not elems:
        if isinstance(value, dict):
            _merge_into(root, value, key_hints)
        return
    parent = _descend(root, elems[:-1], create=True)
    if parent is None:  # pragma: no cover - create=True never returns None
        return
    name, keys = elems[-1]
    if keys:
        node = _as_list_node(parent, name, list(keys)).entry(keys)
        if isinstance(value, dict):
            _merge_into(node, value, key_hints)
        else:
            # A keyed element with a non-dict value is not representable; keep
            # the keys so the entry at least shows up in the materialized list.
            node.clear()
        return
    _set_child(parent, name, value, key_hints)


def _set_child(
    parent: Dict[str, Any],
    name: str,
    value: Any,
    key_hints: Optional[Dict[str, List[str]]],
) -> None:
    """Store *value* under ``parent[name]``, keeping list nodes list nodes."""
    current = parent.get(name)
    key_names: List[str] = []
    if isinstance(current, ListNode):
        key_names = _list_key_names(current)
    if not key_names and key_hints:
        key_names = key_hints.get(name, [])

    if isinstance(value, list) and key_names and _all_dicts(value):
        node = _as_list_node(parent, name, key_names)
        for index, item in enumerate(value):
            if all(k in item for k in key_names):
                item_keys = {k: item[k] for k in key_names}
                # Merge rather than replace: two subscriptions can share an
                # envelope (``/interface[name=*]/statistics`` and
                # ``/interface[name=*]/oper-state`` both return ``interface``),
                # and neither may drop what the other contributed. Entries that
                # go away are cleaned up by HostStream.resync().
                _merge_into(
                    node.entry(item_keys),
                    {k: v for k, v in item.items() if k not in item_keys},
                    key_hints,
                )
            else:
                node.put(f"\x00unkeyed-{index}", {}, dict(item))
        return
    if isinstance(value, dict):
        if isinstance(current, ListNode):
            # A container cannot replace a list node in place; start fresh.
            parent.pop(name, None)
        _merge_into(_as_container(parent, name), value, key_hints)
        return
    parent[name] = value


def _all_dicts(value: List[Any]) -> bool:
    return bool(value) and all(isinstance(item, dict) for item in value)


def _merge_into(
    node: Dict[str, Any],
    value: Dict[str, Any],
    key_hints: Optional[Dict[str, List[str]]] = None,
) -> None:
    """Merge a decoded JSON object into a tree node."""
    for raw_key, raw_val in value.items():
        _set_child(node, strip_module(raw_key), strip_values(raw_val), key_hints)


def strip_values(value: Any) -> Any:
    """Apply module-prefix stripping to a decoded JSON value."""
    if isinstance(value, dict):
        return {strip_module(k): strip_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_values(v) for v in value]
    if isinstance(value, str):
        return strip_module(value)
    return value


def delete(root: Dict[str, Any], path: str) -> None:
    """Remove the node addressed by *path*, if present."""
    elems = parse_path(path)
    if not elems:
        root.clear()
        return
    parent = _descend(root, elems[:-1], create=False)
    if parent is None:
        return
    name, keys = elems[-1]
    if keys:
        current = parent.get(name)
        if isinstance(current, (ListNode, list)):
            node = _as_list_node(parent, name, list(keys))
            node.pop(keys)
            if not len(node):
                parent.pop(name, None)
    else:
        parent.pop(name, None)


def get_node(root: Dict[str, Any], path: str) -> Optional[Any]:
    """Return the node addressed by *path* (``None`` when absent)."""
    elems = parse_path(path)
    if not elems:
        return root
    parent = _descend(root, elems[:-1], create=False)
    if parent is None:
        return None
    name, keys = elems[-1]
    current = parent.get(name)
    if keys:
        if not isinstance(current, ListNode):
            return None
        found = current.entries.get(_key_ident(keys))
        return found[1] if found else None
    return current


def materialize(node: Any) -> Any:
    """Render a tree node back into plain JSON, as a gNMI ``Get`` would."""
    if isinstance(node, ListNode):
        entries = list(node.entries.values())
        return [{**keys, **materialize(child)} for keys, child in entries]
    if isinstance(node, dict):
        items = list(node.items())
        return {k: materialize(v) for k, v in items}
    if isinstance(node, list):
        return copy.deepcopy(node)
    return node


def key_matches(pattern: str, value: str) -> bool:
    """Whether a gNMI key predicate matches a list entry's key value.

    Besides literals and the ``*`` wildcard, SR Linux accepts globs in a key
    value, which the reports use to select a family of entries at once
    (``interface[name=lag*]``).
    """
    if pattern == "*" or pattern == value:
        return True
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatchcase(value, pattern)
    return False


def select_path(value: Any, request: str, envelope: str) -> Any:
    """Restrict a materialized envelope to what the path *request* selects.

    A node's state tree merges every subscription into one structure, so an
    envelope routinely holds more than the requesting path asked for: once
    something subscribes to ``/interface[name=*]/statistics``, every interface
    lives under ``interface``, where a report reading ``/interface[name=lag*]``
    would otherwise see them all. Re-applying the key predicates of *request*
    narrows the view back down to what a real ``Get`` on it would have returned.

    The same merging also leaves whole branches behind that *request* never
    mentions: subscribing to ``.../rib-in-out/rib-in-post/mac-ip-route`` makes
    SR Linux stream its siblings ``rib-in-pre`` and ``rib-out-post`` as well, and
    two reports reading different branches of one envelope each see the other's.
    Getters that walk their response recursively then trip over entries a ``Get``
    would never have handed them, so the branches off the requested path are
    dropped too.

    *envelope* is the path the value sits at, so its length says how much of
    *request* has already been consumed by reaching it.
    """
    elems = parse_path(request)
    depth = len(parse_path(envelope)) if envelope else 0
    if depth == 0:
        selected = _select_children(value, elems)
    else:
        selected = _select(value, elems[depth - 1 :])
    if selected is None:  # nothing along the path survived
        return [] if isinstance(value, list) else {}
    return selected


def _select(value: Any, elems: List[Tuple[str, Dict[str, str]]]) -> Any:
    """Restrict *value*, which stands for ``elems[0]``, to what *elems* select.

    ``None`` means nothing along the path survived, which lets callers leave the
    entry out the way a ``Get`` would have.
    """
    if not elems:
        return value
    value = _matching_entries(value, elems[0][1])
    rest = elems[1:]
    if not rest:
        return value
    if isinstance(value, list):
        kept = [_select_children(item, rest) for item in value]
        return [item for item in kept if item is not None] or None
    return _select_children(value, rest)


def _select_children(container: Any, elems: List[Tuple[str, Dict[str, str]]]) -> Any:
    """Restrict *container* to the child that ``elems[0]`` names.

    Sibling branches are dropped, since a ``Get`` only ever returns the one the
    path points at, and a *container* that does not hold the named child at all
    is dropped with them: the shared tree lists every network-instance an
    unrelated report ever touched, where a ``Get`` for a BGP RIB path would only
    have named the ones that actually run BGP. Leaves stay, since they carry the
    list keys a report projects and cannot confuse a getter that recurses looking
    for dictionaries.
    """
    if not elems or not isinstance(container, dict):
        return container
    name = elems[0][0]
    if name not in container:
        return None
    child = _select(container[name], elems)
    if child is None or child == [] or child == {}:
        return None
    leaves = {k: v for k, v in container.items() if not _is_branch(v)}
    return {**leaves, name: child}


def _is_branch(value: Any) -> bool:
    """Whether *value* is a subtree rather than a leaf or a leaf-list."""
    if isinstance(value, dict):
        return True
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


def _matching_entries(value: Any, keys: Dict[str, str]) -> Any:
    """Drop the list entries that do not match the key predicates *keys*."""
    if not keys or not isinstance(value, list):
        return value
    return [
        item
        for item in value
        if not isinstance(item, dict)
        or all(
            key_matches(pattern, str(item.get(key, "")))
            for key, pattern in keys.items()
        )
    ]
