"""Flatten report getter output into table columns and rows.

This mirrors the flattening the CLI does in :func:`nornir_srl.cli.print_table`
so that a report shown in the browser has the same columns, in the same order,
as the corresponding ``fcli`` command.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..connections.helpers import clean_structured_key

__all__ = ["get_fields", "flatten", "is_scalar"]


def is_scalar(value: Any) -> bool:
    """True for values the table renders directly in a cell."""
    if isinstance(value, (str, int, float)):
        return True
    return isinstance(value, list) and len(value) > 0 and not isinstance(value[0], dict)


def get_fields(item: Any, depth: int = 0) -> List[str]:
    """Derive the column names from the first item of a report result."""
    fields: List[str] = []
    if isinstance(item, list) and len(item) > 0:
        fields.extend(get_fields(item[0], depth=depth + 1))
    elif isinstance(item, dict):
        for key, value in item.items():
            if (
                isinstance(value, list)
                and len(value) > 0
                and isinstance(value[0], dict)
            ):
                fields.extend(get_fields(value[0], depth=depth + 1))
            elif isinstance(value, dict):
                fields.extend(get_fields(value, depth=depth + 1))
            else:
                fields.append(key)
        if depth > 0:
            fields = sorted(fields)
    return fields


def flatten(node: str, items: List[Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Turn one node's report result into ``(columns, rows)``.

    A report item may embed a list of sub-items (e.g. a network-instance with a
    list of routes); each sub-item becomes its own row, repeating the parent's
    scalar fields.
    """
    columns: List[str] = []
    rows: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if not columns:
            columns = get_fields(item)
        common = {k: v for k, v in item.items() if is_scalar(v)}
        nested = [
            (k, v)
            for k, v in item.items()
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict)
        ]
        if not nested:
            rows.append({"Node": node, **common})
            continue
        for _key, sub_items in nested:
            for sub_item in sub_items:
                sub_row = {k: v for k, v in sub_item.items() if is_scalar(v)}
                rows.append({"Node": node, **common, **sub_row})
    return columns, rows


def clean_columns(columns: List[str]) -> List[str]:
    """Strip the ``<n>_`` ordering prefixes used to order table columns."""
    return [clean_structured_key(c) for c in columns]
