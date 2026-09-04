"""Turn report getter output into table columns and rows.

Every surface flattens a getter's result the same way, so that ``fcli mac``,
the ``mac_table`` MCP tool and the MAC Table in the browser all show the same
columns in the same order. A report item may embed a list of sub-items (a
network-instance with a list of routes, say); each sub-item becomes its own row,
inheriting the parent's scalar fields.

The columns are the union of the fields of every item of every node, because
which fields an item carries depends on its state: a route table holds
``orig-vrf`` only on a leaked route, and a node with no routes at all carries
no route fields to name them after.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .connections.helpers import clean_structured_key

#: Called for a host whose report failed. Returns the fields to show for it, or
#: ``None`` to leave it out of the table.
ErrorHandler = Callable[[str, Optional[BaseException]], Optional[Dict[str, Any]]]

__all__ = [
    "ErrorHandler",
    "Row",
    "NodeRows",
    "clean_columns",
    "extract",
    "flatten",
    "get_fields",
    "is_scalar",
    "merge_fields",
    "pass_filter",
    "sub_item_keys",
]


def is_scalar(value: Any) -> bool:
    """True for values the table renders directly in a cell."""
    if isinstance(value, (str, int, float)):
        return True
    return isinstance(value, list) and len(value) > 0 and not isinstance(value[0], dict)


def _scalars(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in item.items() if is_scalar(v)}


def _sub_items(item: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """The nested lists of dicts in *item*, each of which expands into rows."""
    return [
        (k, v)
        for k, v in item.items()
        if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict)
    ]


def merge_fields(columns: List[str], fields: Iterable[str]) -> List[str]:
    """Append the fields of *fields* that *columns* does not have yet, in place."""
    for name in fields:
        if name not in columns:
            columns.append(name)
    return columns


def sub_item_keys(items: Iterable[Any]) -> Set[str]:
    """The keys of *items* that hold sub-items rather than a value.

    ``Rib`` in a route table names the list of routes; it groups the rows, it is
    never a cell. But an item for a network-instance with no routes holds it as
    a null, indistinguishable from a field, so it takes an item that *has*
    routes to know it is not one. The nodes of a fabric disagree about that -
    only some have IPv6 routes - so a caller collects these over every node
    before deciding which columns to keep.
    """
    return {
        key for item in items if isinstance(item, dict) for key, _ in _sub_items(item)
    }


def get_fields(item: Any, depth: int = 0) -> List[str]:
    """Derive the column names from one item of a report result.

    Fields are unioned over every sub-item of a nested list, because a route or
    neighbour only carries the fields that apply to it: the first entry of a
    route table has no ``orig-vrf`` unless it happens to be a leaked route.
    """
    fields: List[str] = []
    if isinstance(item, list):
        for element in item:
            merge_fields(fields, get_fields(element, depth=depth + 1))
    elif isinstance(item, dict):
        for key, value in item.items():
            if (
                isinstance(value, list)
                and len(value) > 0
                and isinstance(value[0], dict)
            ):
                # A set, because a route table runs to thousands of sub-items
                # and the union is taken over every one of them.
                nested: Set[str] = set()
                for sub_item in value:
                    nested.update(get_fields(sub_item, depth=depth + 1))
                merge_fields(fields, sorted(nested))
            elif isinstance(value, dict):
                merge_fields(fields, get_fields(value, depth=depth + 1))
            else:
                merge_fields(fields, (key,))
        if depth > 0:
            fields = sorted(fields)
    return fields


def clean_columns(columns: Iterable[str]) -> List[str]:
    """Strip the ``<n>_`` prefixes that only exist to order table columns."""
    return [clean_structured_key(c) for c in columns]


def cell(value: Any) -> Any:
    """Render one value into something JSON- and table-friendly.

    Shared, because a table saved as a snapshot on one surface is compared
    against one rendered on another, and two spellings of the same list would
    read as a change.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def pass_filter(row: Dict[str, Any], filter: Optional[Dict[str, Any]]) -> bool:
    """True when *row* matches every ``field=regex`` pair in *filter*.

    Keys are matched case-insensitively, values as case-insensitive regexes. A
    filter key that no field matches rejects the row.
    """
    if not filter:
        return True
    wanted = {str(k).lower(): v for k, v in filter.items()}
    matched = {
        str(k).lower()
        for k, v in row.items()
        if wanted.get(str(k).lower())
        and re.search(str(wanted[str(k).lower()]), str(v), re.IGNORECASE)
    }
    return len(matched) >= len(wanted)


@dataclass
class Row:
    """One table row, with what a renderer needs to group it visually."""

    values: Dict[str, Any]
    #: Fields inherited from the parent item, which a table blanks out on
    #: continuation rows so the parent reads as a single entry spanning them.
    inherited: Tuple[str, ...] = ()
    #: True when this row continues the same parent item as the row before it.
    continues: bool = False

    def cells(self, *, group: bool = False) -> Dict[str, Any]:
        """The fields to render; *group* drops values repeated from the row above."""
        if group and self.continues:
            return {k: v for k, v in self.values.items() if k not in self.inherited}
        return self.values


@dataclass
class NodeRows:
    """The rows one node contributed to a report."""

    node: str
    rows: List[Row] = field(default_factory=list)


def _expand(item: Dict[str, Any], filter: Optional[Dict[str, Any]]) -> List[Row]:
    """Expand one report item into the rows it represents."""
    common = _scalars(item)
    nested = _sub_items(item)
    if not nested:
        return [Row(common)] if pass_filter(common, filter) else []

    rows: List[Row] = []
    for _key, sub_items in nested:
        first = True
        for sub_item in sub_items:
            scalars = _scalars(sub_item)
            merged = {**common, **scalars}
            if not pass_filter(merged, filter):
                continue
            # A field the sub-item carries itself is its own, even where the
            # parent has one by the same name, so it is never blanked out.
            inherited = tuple(k for k in common if k not in scalars)
            rows.append(Row(merged, inherited=inherited, continues=not first))
            first = False
    return rows


def extract(
    resource: str,
    results: Any,
    *,
    field_filter: Optional[Dict[str, Any]] = None,
    on_error: Optional[ErrorHandler] = None,
) -> Tuple[List[str], List[NodeRows]]:
    """Flatten a Nornir ``AggregatedResult`` into ``(columns, rows per node)``.

    Columns are the union over every item of every node, so all nodes line up
    on the same schema and a field is not lost because the node that happened
    to be first has no row carrying it. Failed hosts are handed to *on_error*,
    which either returns the fields to show for them or ``None`` to leave them
    out.
    """
    columns: List[str] = []
    containers: Set[str] = set()
    per_node: List[NodeRows] = []

    for host, host_result in results.items():
        result = host_result[0]
        node = result.host
        name = (node.hostname or node.name) if node else host
        if result.failed:
            values = on_error(name, result.exception) if on_error else None
            if values:
                per_node.append(NodeRows(name, [Row(values)]))
            continue
        items = (result.result or {}).get(resource)
        if not items:
            continue
        rows: List[Row] = []
        containers |= sub_item_keys(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            merge_fields(columns, get_fields(item))
            rows.extend(_expand(item, field_filter))
        per_node.append(NodeRows(name, rows))

    return [c for c in columns if c not in containers], per_node


def flatten(node: str, items: List[Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Turn one node's report result into ``(columns, rows)``.

    Used by the streaming server, which already has the result for a single node
    rather than an ``AggregatedResult``, and merges the columns of every node
    itself. The sub-item keys this node cannot recognise as such are left in the
    columns for the server to drop once it has heard from every node - see
    :func:`sub_item_keys`.
    """
    columns: List[str] = []
    rows: List[Dict[str, Any]] = []
    containers = sub_item_keys(items or [])
    for item in items or []:
        if not isinstance(item, dict):
            continue
        merge_fields(columns, get_fields(item))
        for row in _expand(item, None):
            rows.append({"Node": node, **row.values})
    return [c for c in columns if c not in containers], rows
