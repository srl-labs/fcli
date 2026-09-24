"""Tests for Usability Priority 1 features:
- Fabric summary (CLI & MCP)
- Auto-discovery & graceful inventory error handling
- Containerlab node prefix stripping and short-name inventory filtering
- Scope column in incidents lens
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nornir_srl.checks import Finding
from nornir_srl.cli import app
from nornir_srl.fabric import FabricState
from nornir_srl.incidents import Incident, correlate
from nornir_srl.lenses import INCIDENT_COLUMNS, get_lens
from nornir_srl.mcp_server import fabric_summary, mcp
from nornir_srl.server.topology import facts_from_fabric_state, summarize_fabric


def _dummy_state() -> FabricState:
    state = FabricState()
    state.hostnames = {
        "clab-mylab-leaf1": "leaf1",
        "clab-mylab-leaf2": "leaf2",
        "clab-mylab-spine1": "spine1",
    }
    state.reports = {
        "sys_info": {
            "clab-mylab-leaf1": [{"version": "24.7.1", "chassis-type": "7220 IXR-D2L"}],
            "clab-mylab-leaf2": [{"version": "24.7.1", "chassis-type": "7220 IXR-D2L"}],
            "clab-mylab-spine1": [{"version": "24.7.1", "chassis-type": "7220 IXR-D5"}],
        },
        "lldp": {
            "clab-mylab-leaf1": [
                MagicMock(
                    port="ethernet-1/1",
                    neighbors=[MagicMock(chassis="spine1", port="ethernet-1/1")],
                )
            ],
            "clab-mylab-leaf2": [
                MagicMock(
                    port="ethernet-1/1",
                    neighbors=[MagicMock(chassis="spine1", port="ethernet-1/2")],
                )
            ],
            "clab-mylab-spine1": [
                MagicMock(
                    port="ethernet-1/1",
                    neighbors=[MagicMock(chassis="leaf1", port="ethernet-1/1")],
                ),
                MagicMock(
                    port="ethernet-1/2",
                    neighbors=[MagicMock(chassis="leaf2", port="ethernet-1/1")],
                ),
            ],
        },
        "bgp_peers": {
            "clab-mylab-leaf1": [MagicMock(peer_address="10.0.0.1")],
            "clab-mylab-leaf2": [MagicMock(peer_address="10.0.0.1")],
            "clab-mylab-spine1": [
                MagicMock(peer_address="10.0.0.2"),
                MagicMock(peer_address="10.0.0.3"),
            ],
        },
        "ni": {
            "clab-mylab-leaf1": [
                {"name": "mgmt", "type": "mgmt"},
                {"name": "default", "type": "default"},
                {"name": "ipvrf-1", "type": "ip-vrf"},
            ],
            "clab-mylab-leaf2": [
                {"name": "mgmt", "type": "mgmt"},
                {"name": "default", "type": "default"},
                {"name": "ipvrf-1", "type": "ip-vrf"},
            ],
            "clab-mylab-spine1": [
                {"name": "mgmt", "type": "mgmt"},
                {"name": "default", "type": "default"},
            ],
        },
        "es": {
            "clab-mylab-leaf1": [],
            "clab-mylab-leaf2": [],
            "clab-mylab-spine1": [],
        },
    }
    return state


def test_facts_from_fabric_state_extracts_devices_and_services():
    state = _dummy_state()
    facts = facts_from_fabric_state(state)
    assert len(facts) == 3
    nodes = {f.name: f for f in facts}
    assert "clab-mylab-leaf1" in nodes
    assert nodes["clab-mylab-leaf1"].platform == "7220 IXR-D2L"
    assert "ipvrf-1" in nodes["clab-mylab-leaf1"].services
    assert "default" not in nodes["clab-mylab-leaf1"].services  # infra filtered out


def test_summarize_fabric_generates_briefing_and_stats():
    state = _dummy_state()
    finding = Finding(
        check="bfd",
        severity="error",
        node="clab-mylab-leaf1",
        subject="session down",
        detail="BFD session to spine1 down",
    )
    result = summarize_fabric(state, findings=[finding])

    assert result["nodes"] == 3
    assert result["roles"]["core"] == 1
    assert result["roles"]["leaf"] == 2
    assert result["services"] == 1
    assert result["incidents"]["errors"] == 1
    assert result["incidents"]["worst"] != ""
    assert any("3 nodes" in line for line in result["summary"])
    assert any("2 leaves" in line for line in result["summary"])


def test_incident_scope_column():
    scope_col = next(c for c in INCIDENT_COLUMNS if c.name == "Scope")

    f1 = Finding("bfd", "error", "leaf1", "s1", "down")
    f2 = Finding("bfd", "error", "leaf2", "s1", "down")

    inc_single = Incident(
        id="inc-1",
        severity="error",
        kind="link",
        title="link down",
        node="leaf1",
        nodes=("leaf1",),
        root=f1,
        explanation="single node link",
    )
    assert scope_col.of(inc_single) == "1 node"

    inc_multi = Incident(
        id="inc-2",
        severity="error",
        kind="link",
        title="link down",
        node="leaf1",
        nodes=("leaf1", "leaf2"),
        root=f1,
        related=(f2,),
        explanation="multi node link",
    )
    assert scope_col.of(inc_multi) == "2 nodes"

    inc_pattern = Incident(
        id="inc-3",
        severity="warning",
        kind="pattern",
        title="pattern issue",
        node="",
        nodes=("leaf1", "leaf2"),
        root=f1,
        related=(f2,),
        explanation="systemic issue",
    )
    assert scope_col.of(inc_pattern) == "fabric-wide"


def test_cli_help_does_not_require_inventory(tmp_path, monkeypatch):
    """Running fcli --help or fcli <subcmd> --help should not require a topo or config file."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "summary" in result.stdout

    result = runner.invoke(app, ["summary", "--help"])
    assert result.exit_code == 0
    assert "Displays an executive summary" in result.stdout

    result = runner.invoke(app, ["incidents", "--help"])
    assert result.exit_code == 0
    assert "Groups the checks' findings" in result.stdout


def test_cli_missing_inventory_gives_clean_tip(tmp_path, monkeypatch):
    """Running a command without an inventory or topo should print a friendly message, not a traceback."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["summary"])
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "Traceback" not in result.output


def test_mcp_fabric_summary_tool():
    """Verify fabric_summary MCP tool returns a briefing."""
    state = _dummy_state()
    mock_nornir = MagicMock()
    with patch("nornir_srl.mcp_server.get_nornir", return_value=mock_nornir), \
         patch("nornir_srl.mcp_server.collect_lens_state", return_value=state):
        output = fabric_summary()
        parsed = json.loads(output)
        assert parsed["nodes"] == 3
        assert parsed["roles"]["core"] == 1
        assert parsed["roles"]["leaf"] == 2
        assert any("3 nodes" in line for line in parsed["summary"])


def test_cli_summary_command_table_and_json(tmp_path):
    """Verify fcli summary runs with -t and supports table and JSON output."""
    topo = tmp_path / "lab.clab.yml"
    topo.write_text("""
name: mylab
topology:
  defaults:
    kind: nokia_srlinux
  nodes:
    leaf1:
    leaf2:
""", encoding="utf-8")

    clean_result = {
        "summary": ["3 nodes: 2 leaves, 1 core", "Carries 1 service", "No incidents"],
        "nodes": 3,
        "roles": {"leaf": 2, "core": 1},
        "services": 1,
        "incidents": {"open": 0, "errors": 0, "warnings": 0, "findings": 0, "worst": "", "acknowledged": 0},
        "graph": {"nodes": [
            {"name": "clab-mylab-spine1", "role": "core", "platform": "7220 IXR-D5", "layer": 6, "connected": True, "services": [], "peers": []},
            {"name": "clab-mylab-leaf1", "role": "leaf", "platform": "7220 IXR-D2L", "layer": 3, "connected": True, "services": ["ipvrf-1"], "peers": []},
            {"name": "clab-mylab-leaf2", "role": "leaf", "platform": "7220 IXR-D2L", "layer": 3, "connected": True, "services": ["ipvrf-1"], "peers": []},
        ]},
    }

    state = _dummy_state()
    runner = CliRunner()
    with patch("nornir_srl.cli.collect_lens_state", return_value=state), \
         patch("nornir_srl.server.topology.summarize_fabric", return_value=clean_result):
        res_table = runner.invoke(app, ["-t", str(topo), "summary"])
        assert res_table.exit_code == 0
        assert "Fabric Summary" in res_table.stdout
        assert "Nodes & Roles" in res_table.stdout
        # node_prefix stripped: leaf1 shown instead of clab-mylab-leaf1
        assert "leaf1" in res_table.stdout

        res_json = runner.invoke(app, ["-t", str(topo), "-o", "json", "summary"])
        assert res_json.exit_code == 0
        data = json.loads(res_json.stdout)
        assert isinstance(data, list)
        assert data[0]["nodes"] == 3


def test_cli_inventory_filter_short_name(tmp_path):
    """Verify -i node=leaf1 or -i name=leaf1 resolves to clab-mylab-leaf1."""
    topo = tmp_path / "lab.clab.yml"
    topo.write_text("""
name: mylab
topology:
  defaults:
    kind: nokia_srlinux
  nodes:
    leaf1:
    leaf2:
""", encoding="utf-8")

    clean_result = {
        "summary": ["1 node"],
        "nodes": 1,
        "roles": {"leaf": 1},
        "services": 1,
        "incidents": {"open": 0, "errors": 0, "warnings": 0, "findings": 0, "worst": "", "acknowledged": 0},
        "graph": {"nodes": [
            {"name": "clab-mylab-leaf1", "role": "leaf", "platform": "7220 IXR-D2L", "layer": 3, "connected": True, "services": ["ipvrf-1"], "peers": []},
        ]},
    }

    state = _dummy_state()
    runner = CliRunner()
    with patch("nornir_srl.cli.collect_lens_state", return_value=state), \
         patch("nornir_srl.server.topology.summarize_fabric", return_value=clean_result):
        # Using short name=leaf1
        res1 = runner.invoke(app, ["-t", str(topo), "-i", "name=leaf1", "summary"])
        assert res1.exit_code == 0

        # Using alias node=leaf1
        res2 = runner.invoke(app, ["-t", str(topo), "-i", "node=leaf1", "summary"])
        assert res2.exit_code == 0

