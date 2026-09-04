"""Comparing two renderings of the same report.

Two questions come up on every incident. What changed since this fabric was
working, and why does this leaf not look like the one beside it. They are the
same question asked of two tables, so they are one function here: the fabric a
snapshot was taken of and the fabric now, or node A and node B of the fabric in
front of you.

A table is compared row by row, and a row is identified by the columns that name
it rather than by the ones that describe it - the peer address rather than the
number of routes it has sent. Those columns are declared per report as
:attr:`~nornir_srl.reports.ReportSpec.key_columns`. A report that declares none
is still comparable: without a way to tell a changed row from a new one, every
difference simply reads as one row gone and another arrived.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: A row only the second table has.
ADDED = "added"
#: A row only the first table has.
REMOVED = "removed"
#: A row both have, with something different about it.
CHANGED = "changed"
#: A row both have, identical.
SAME = "same"

#: The column holding a row's verdict. Narrow, and hard to collide with a
#: column a report of its own might have.
STATUS = "±"

#: How a changed cell reads: what it was, and what it is.
_ARROW = " \u2192 "

_ORDER = {REMOVED: 0, ADDED: 1, CHANGED: 2, SAME: 3}


def _key_of(
    row: Mapping[str, Any], key_columns: Sequence[str], columns: Sequence[str]
) -> Tuple[Any, ...]:
    """What identifies *row*, as a tuple that can be looked up.

    Falls back to the whole row when no usable key column was declared, which
    can only ever report a row as added or removed - never as changed - and is
    the honest answer when nothing says which rows are the same row.
    """
    usable = [c for c in key_columns if c in columns]
    if not usable:
        return tuple(str(row.get(c, "")) for c in columns)
    return tuple(str(row.get(c, "")) for c in usable)


def _bucket(
    rows: Iterable[Mapping[str, Any]],
    key_columns: Sequence[str],
    columns: Sequence[str],
) -> Dict[Tuple[Any, ...], List[Mapping[str, Any]]]:
    """Group rows by identity.

    A list rather than a single row per key: a key that does not fully identify
    a row is a key several rows can share, and dropping the extras would report
    them as missing.
    """
    buckets: Dict[Tuple[Any, ...], List[Mapping[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(_key_of(row, key_columns, columns), []).append(row)
    return buckets


def _changes(
    before: Mapping[str, Any], after: Mapping[str, Any], columns: Sequence[str]
) -> Dict[str, List[Any]]:
    """The columns whose value is not the same in both, as ``[was, is]``."""
    return {
        column: [before.get(column), after.get(column)]
        for column in columns
        if str(before.get(column, "")) != str(after.get(column, ""))
    }


def _merged_columns(before: Sequence[str], after: Sequence[str]) -> List[str]:
    """Every column either table has, in the order the second one reads."""
    merged = list(after)
    merged.extend(column for column in before if column not in merged)
    return merged


def diff_rows(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    key_columns: Sequence[str] = (),
    *,
    include_same: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Compare two sets of rows, and count what came of it.

    Returns the rows of the comparison and a tally by verdict. A changed cell
    reads as ``was -> is`` so it needs nothing of whoever renders it, and the
    structured form is kept under ``_changes`` for whoever wants it.
    """
    old = _bucket(before, key_columns, columns)
    new = _bucket(after, key_columns, columns)
    counts = {ADDED: 0, REMOVED: 0, CHANGED: 0, SAME: 0}
    result: List[Dict[str, Any]] = []

    # The second table's order, then whatever the first had and it does not.
    keys = list(new) + [key for key in old if key not in new]
    for key in keys:
        was, now = old.get(key, []), new.get(key, [])
        # Rows sharing a key are paired off in the order they were rendered,
        # which is the only order either table gives them.
        for before_row, after_row in zip(was, now):
            changed = _changes(before_row, after_row, columns)
            if not changed:
                counts[SAME] += 1
                if include_same:
                    result.append({STATUS: SAME, **dict(after_row)})
                continue
            counts[CHANGED] += 1
            row: Dict[str, Any] = dict(after_row)
            for column, (old_value, new_value) in changed.items():
                row[column] = f"{_display(old_value)}{_ARROW}{_display(new_value)}"
            result.append({STATUS: CHANGED, **row, "_changes": changed})
        for extra in was[len(now) :]:
            counts[REMOVED] += 1
            result.append({STATUS: REMOVED, **dict(extra)})
        for extra in now[len(was) :]:
            counts[ADDED] += 1
            result.append({STATUS: ADDED, **dict(extra)})

    result.sort(key=lambda row: _ORDER.get(str(row[STATUS]), 9))
    return result, counts


def _display(value: Any) -> str:
    return "" if value is None else str(value)


def diff_tables(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    key_columns: Sequence[str] = (),
    *,
    labels: Tuple[str, str] = ("before", "after"),
    include_same: bool = False,
    drop_columns: Sequence[str] = (),
) -> Dict[str, Any]:
    """Compare two rendered tables, as one table of what differs.

    The result is a table like any other - the same keys, the same row shape -
    with a verdict column in front, so it renders wherever a report does.

    *drop_columns* are left out of the comparison and of the result, which is
    what makes one node comparable with another: ``Node`` is the one column two
    nodes are guaranteed to disagree about.
    """
    dropped = {c.lower() for c in drop_columns}
    columns = [
        c
        for c in _merged_columns(before.get("columns") or [], after.get("columns") or [])
        if c.lower() not in dropped
    ]
    keys = [c for c in key_columns if c.lower() not in dropped]
    rows, counts = diff_rows(
        [_without(r, dropped) for r in before.get("rows") or []],
        [_without(r, dropped) for r in after.get("rows") or []],
        columns,
        keys,
        include_same=include_same,
    )
    report = after.get("report") or before.get("report") or ""
    title = after.get("title") or before.get("title") or report
    return {
        "report": report,
        "title": f"{title}: {labels[0]} vs {labels[1]}",
        "columns": [STATUS] + columns,
        "rows": rows,
        "errors": list(before.get("errors") or []) + list(after.get("errors") or []),
        "nodes": after.get("nodes", 0),
        "generated": after.get("generated"),
        "diff": {
            "labels": list(labels),
            "counts": counts,
            "key_columns": keys,
            # False when nothing identified a row, so 'changed' was never
            # something this comparison could report.
            "keyed": bool([c for c in keys if c in columns]),
            "include_same": include_same,
        },
    }


def _without(row: Mapping[str, Any], dropped: Sequence[str]) -> Dict[str, Any]:
    if not dropped:
        return dict(row)
    return {k: v for k, v in row.items() if k.lower() not in dropped}


def split_by_node(
    table: Mapping[str, Any], node_column: str = "Node"
) -> Dict[str, Dict[str, Any]]:
    """One table per node, out of a table covering the fabric."""
    per_node: Dict[str, Dict[str, Any]] = {}
    for row in table.get("rows") or []:
        node = str(row.get(node_column, ""))
        entry = per_node.setdefault(
            node,
            {
                "report": table.get("report"),
                "title": table.get("title"),
                "columns": list(table.get("columns") or []),
                "rows": [],
                "errors": [],
                "nodes": 1,
                "generated": table.get("generated"),
            },
        )
        entry["rows"].append(dict(row))
    return per_node


def diff_nodes(
    table: Mapping[str, Any],
    first: str,
    second: str,
    key_columns: Sequence[str] = (),
    *,
    include_same: bool = False,
    node_column: str = "Node",
) -> Dict[str, Any]:
    """Compare what two nodes of one fabric make of the same report.

    The comparison a redundant pair asks for: the two halves of a multi-homed
    leaf pair should differ in their addresses and in nothing else.
    """
    per_node = split_by_node(table, node_column)
    missing = [node for node in (first, second) if node not in per_node]
    empty = {
        "report": table.get("report"),
        "title": table.get("title"),
        "columns": list(table.get("columns") or []),
        "rows": [],
        "errors": [],
        "nodes": 1,
        "generated": table.get("generated"),
    }
    result = diff_tables(
        per_node.get(first, empty),
        per_node.get(second, empty),
        key_columns,
        labels=(first, second),
        include_same=include_same,
        drop_columns=(node_column,),
    )
    if missing:
        # A node with no rows and a node that is not in the table look the same
        # in the comparison, and only one of them means what it seems to.
        result["errors"] = list(result["errors"]) + [
            {"node": node, "error": "no rows in this report"} for node in missing
        ]
    return result


def key_columns_for(report: Any) -> Tuple[str, ...]:
    """The key columns of a report, as the rendered table spells them.

    Getters name a field for the table it will become - ``1_peer`` sorts the
    column, ``"U4\\nR/A/T"`` wraps its header - and the rendering strips both
    before anyone sees it. Declaring keys as they are rendered is what lets a
    saved table be compared with a fresh one.
    """
    return tuple(getattr(report, "key_columns", ()) or ())


__all__ = [
    "ADDED",
    "CHANGED",
    "REMOVED",
    "SAME",
    "STATUS",
    "diff_nodes",
    "diff_rows",
    "diff_tables",
    "key_columns_for",
    "split_by_node",
]
