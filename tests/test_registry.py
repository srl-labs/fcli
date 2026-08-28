"""Tests for the shared report registry and the row flattening it feeds.

The registry exists so a report is declared once and every surface renders it
the same way. These tests fail if a surface starts drifting from it again.
"""

import inspect

import pytest
from nornir.core.inventory import Host
from nornir.core.task import AggregatedResult, MultiResult, Result

from nornir_srl import cli, mcp_server
from nornir_srl.reports import (
    ALL_SURFACES,
    CLI,
    MCP,
    SERVER,
    REPORTS,
    get_report,
    reports_for,
)
from nornir_srl.rows import extract, pass_filter


# --------------------------------------------------------------------------- #
# Registry consistency
# --------------------------------------------------------------------------- #


def test_report_names_and_tool_names_are_unique():
    names = [r.name for r in REPORTS]
    assert len(names) == len(set(names))
    tool_names = [r.tool_name for r in reports_for(MCP)]
    assert len(tool_names) == len(set(tool_names))


def test_every_report_is_offered_somewhere():
    for report in REPORTS:
        assert report.surfaces, f"{report.name} is on no surface"
        assert report.surfaces <= ALL_SURFACES


def test_every_getter_is_callable_with_only_a_device():
    """A surface with nothing to pass - the server - must still be able to call."""
    for report in REPORTS:
        params = list(inspect.signature(report.getter).parameters.values())
        assert params, f"{report.name} getter takes no device"
        for param in params[1:]:
            assert (
                param.default is not inspect.Parameter.empty
            ), f"{report.name} getter requires '{param.name}'"


def test_non_tabular_reports_are_not_streamed():
    """The browser only shows tables, so a report without columns cannot stream."""
    for report in REPORTS:
        if not report.tabular:
            assert not report.on(SERVER)


# --------------------------------------------------------------------------- #
# Surfaces match the registry
# --------------------------------------------------------------------------- #


def _cli_commands():
    return {c.callback.__name__: c for c in cli.app.registered_commands}


def test_cli_exposes_exactly_the_cli_reports():
    commands = _cli_commands()
    expected = {r.name for r in reports_for(CLI)}
    # 'server' is a command but not a report.
    assert expected <= set(commands)
    assert set(commands) - expected == {"server"}


def test_mcp_exposes_exactly_the_mcp_reports():
    for report in reports_for(MCP):
        tool = getattr(mcp_server, report.tool_name, None)
        assert tool is not None, f"no MCP tool for report '{report.name}'"
        assert tool.__doc__, f"MCP tool '{report.tool_name}' has no description"


def test_server_reports_all_declare_a_resource():
    for report in reports_for(SERVER):
        assert report.resource
        assert report.sample_interval > 0


def test_get_report_rejects_unknown_names():
    with pytest.raises(KeyError):
        get_report("no-such-report")


def test_subscriptions_convert_to_gnmi_sample_intervals():
    spec = get_report("bgp_peers").subscribe[0]
    assert spec.as_gnmi() == {
        "path": spec.path,
        "mode": "sample",
        "sample_interval": 10_000_000_000,
    }


# --------------------------------------------------------------------------- #
# Shared row extraction
# --------------------------------------------------------------------------- #


def _aggregated(resource, per_host, failed=()):
    """Build an AggregatedResult like a Nornir run would."""
    aggregated = AggregatedResult(resource)
    for name, items in per_host.items():
        result = Result(host=Host(name=name, hostname=name), result={resource: items})
        multi = MultiResult(resource)
        multi.append(result)
        aggregated[name] = multi
    for name, exception in failed:
        result = Result(host=Host(name=name, hostname=name), exception=exception)
        result.failed = True
        multi = MultiResult(resource)
        multi.append(result)
        aggregated[name] = multi
    return aggregated


def test_extract_takes_columns_from_the_first_item():
    results = _aggregated("ni", {"leaf1": [{"ni": "default", "oper-state": "up"}]})
    columns, per_node = extract("ni", results)
    assert columns == ["ni", "oper-state"]
    assert [r.values for r in per_node[0].rows] == [
        {"ni": "default", "oper-state": "up"}
    ]


def test_extract_expands_sub_items_into_rows_inheriting_the_parent():
    items = [
        {
            "ni": "default",
            "itfs": [{"itf": "ethernet-1/1"}, {"itf": "ethernet-1/2"}],
        }
    ]
    columns, per_node = extract("ni", _aggregated("ni", {"leaf1": items}))
    assert columns == ["ni", "itf"]
    rows = per_node[0].rows
    assert [r.values for r in rows] == [
        {"ni": "default", "itf": "ethernet-1/1"},
        {"ni": "default", "itf": "ethernet-1/2"},
    ]
    # A table shows the parent once and blanks it on the rows that continue it,
    # while structured output repeats it on every row.
    assert rows[0].cells(group=True) == {"ni": "default", "itf": "ethernet-1/1"}
    assert rows[1].cells(group=True) == {"itf": "ethernet-1/2"}
    assert rows[1].cells() == {"ni": "default", "itf": "ethernet-1/2"}


def test_a_sub_item_keeps_a_field_the_parent_also_has():
    """Reports reuse names like 'oper-state' at both levels; the sub-item wins."""
    items = [
        {
            "ni": "default",
            "oper-state": "up",
            "itfs": [
                {"itf": "ethernet-1/1", "oper-state": "up"},
                {"itf": "ethernet-1/2", "oper-state": "down"},
            ],
        }
    ]
    _columns, per_node = extract("ni", _aggregated("ni", {"leaf1": items}))
    continued = per_node[0].rows[1].cells(group=True)
    assert continued == {"itf": "ethernet-1/2", "oper-state": "down"}
    assert "ni" not in continued


def test_extract_applies_the_field_filter_to_parent_and_sub_item_fields():
    items = [
        {
            "ni": "default",
            "itfs": [
                {"itf": "ethernet-1/1", "oper-state": "up"},
                {"itf": "ethernet-1/2", "oper-state": "down"},
            ],
        }
    ]
    _columns, per_node = extract(
        "ni", _aggregated("ni", {"leaf1": items}), field_filter={"oper-state": "down"}
    )
    assert [r.values["itf"] for r in per_node[0].rows] == ["ethernet-1/2"]


def test_extract_hands_failed_hosts_to_the_callback():
    results = _aggregated(
        "ni", {"leaf1": [{"ni": "default"}]}, failed=[("leaf2", RuntimeError("boom"))]
    )
    seen = []

    def on_error(node, exception):
        seen.append((node, str(exception)))
        return {"_error": str(exception)}

    _columns, per_node = extract("ni", results, on_error=on_error)
    assert seen == [("leaf2", "boom")]
    assert per_node[1].rows[0].values == {"_error": "boom"}


def test_extract_leaves_failed_hosts_out_when_the_callback_returns_nothing():
    results = _aggregated("ni", {}, failed=[("leaf2", RuntimeError("boom"))])
    _columns, per_node = extract("ni", results, on_error=lambda node, exc: None)
    assert per_node == []


def test_pass_filter_matches_case_insensitive_regexes():
    row = {"oper-state": "Down", "itf": "ethernet-1/1"}
    assert pass_filter(row, None)
    assert pass_filter(row, {"Oper-State": "down"})
    assert pass_filter(row, {"itf": "ethernet-1/."})
    assert not pass_filter(row, {"oper-state": "up"})
    # Every filter key has to match something.
    assert not pass_filter(row, {"oper-state": "down", "missing": "x"})
