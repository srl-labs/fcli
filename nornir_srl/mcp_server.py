"""
MCP Server for fcli - exposes SR Linux fabric reports as MCP tools.

Supports both stdio and HTTP (SSE) transports.

Usage:
    # stdio transport (default)
    fcli-mcp

    # HTTP transport
    fcli-mcp --transport http --host 0.0.0.0 --port 8080

    # With containerlab topology file
    fcli-mcp --topo-file <path-to-topo.yml>

    # With nornir config file
    fcli-mcp --config-file nornir_config.yaml

    # With inventory filter
    fcli-mcp --topo-file <path> --inv-filter role=leaf --inv-filter site=dc1
"""

import argparse
import atexit
import glob
import json
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Literal

import yaml  # type: ignore[import-untyped]
from mcp.server.fastmcp import FastMCP
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.task import Result, Task

from .connections.srlinux import CONNECTION_NAME
from .connections.helpers import clean_structured_key
from .reports import ReportSpec, get_report
from .rows import extract

logger = logging.getLogger(__name__)

SRL_DEFAULT_USERNAME = "admin"
SRL_DEFAULT_PASSWORD = "NokiaSrl1!"
SRL_DEFAULT_GNMI_PORT = 57400

NORNIR_DEFAULT_CONFIG: Dict[str, Any] = {
    "inventory": {
        "plugin": "YAMLInventory",
        "options": {
            "host_file": "clab_hosts.yml",
            "group_file": "clab_groups.yml",
            "defaults_file": "clab_defaults.yml",
        },
    },
    "runner": {"plugin": "threaded", "options": {"num_workers": 20}},
    "user_defined": {"intent_dir": "intent"},
    "logging": {"enabled": False},
}


# ---- Nornir initialization ----

# These hold the initialized nornir instance and persistent temp files
_nornir_instance: Optional[Nornir] = None
_temp_files: List[Any] = []  # prevent GC of NamedTemporaryFile objects


def _cleanup_temp_files() -> None:
    """Clean up any temporary files created during initialization."""
    for f in _temp_files:
        try:
            if os.path.exists(f.name):
                os.unlink(f.name)
        except Exception as e:
            logger.debug("Failed to clean up temp file %s: %s", f.name, e)


atexit.register(_cleanup_temp_files)


def _init_nornir_from_topo(
    topo_file: str,
    cert_file: Optional[str] = None,
    gnmi_port: int = SRL_DEFAULT_GNMI_PORT,
) -> Nornir:
    """Initialize Nornir from a containerlab topology file."""
    with open(topo_file, "r") as f:
        topo = yaml.safe_load(os.path.expandvars(f.read()))

    lab_name = topo["name"]
    if "prefix" not in topo:
        prefix = f"clab-{lab_name}-"
    else:
        if topo["prefix"] == "__lab-name":
            prefix = f"{lab_name}-"
        elif topo["prefix"] == "":
            prefix = ""
        else:
            prefix = f"{topo['prefix']}-{lab_name}-"

    hosts: Dict[str, Dict[str, Any]] = {}
    def_kind = topo["topology"].get("defaults", {}).get("kind")
    def_image = (
        topo["topology"].get("defaults", {}).get("image")
        or topo["topology"]["kinds"].get(def_kind, {}).get("image")
        if def_kind
        else None
    )
    srlinux_def = False
    if def_image and "srlinux" in def_image:
        srlinux_def = True
    if def_kind in {"srl", "nokia_srlinux"}:
        srlinux_def = True

    srl_kinds = [
        k
        for k, v in topo["topology"].get("kinds", {}).items()
        if "/srlinux" in v.get("image", "")
    ]
    for extra in ("srl", "nokia_srlinux"):
        if extra not in srl_kinds:
            srl_kinds.append(extra)

    clab_nodes: Dict[str, Dict] = topo["topology"]["nodes"]
    for node, node_spec in clab_nodes.items():
        node_kind = node_spec.get("kind")
        if (node_kind is None and srlinux_def) or node_kind in srl_kinds:
            hosts[f"{prefix}{node}"] = {
                "hostname": f"{prefix}{node}",
                "platform": "srlinux",
                "groups": ["srl"],
                "data": node_spec.get("labels", {}),
            }

    groups: Dict[str, Dict[str, Any]] = {
        "srl": {
            "connection_options": {
                "srlinux": {
                    "username": SRL_DEFAULT_USERNAME,
                    "password": SRL_DEFAULT_PASSWORD,
                    "port": gnmi_port,
                    "extras": {},
                }
            }
        }
    }
    if cert_file:
        groups["srl"]["connection_options"]["srlinux"]["extras"][
            "path_cert"
        ] = cert_file

    hosts_f = tempfile.NamedTemporaryFile("w+", suffix=".yml", delete=False)
    yaml.safe_dump(hosts, hosts_f)
    hosts_f.seek(0)
    _temp_files.append(hosts_f)

    groups_f = tempfile.NamedTemporaryFile("w+", suffix=".yml", delete=False)
    yaml.safe_dump(groups, groups_f)
    groups_f.seek(0)
    _temp_files.append(groups_f)

    conf: Dict[str, Any] = dict(NORNIR_DEFAULT_CONFIG)
    conf["inventory"] = {
        "options": {
            "host_file": hosts_f.name,
            "group_file": groups_f.name,
        }
    }
    return InitNornir(**conf)


def _init_nornir_from_config(config_file: str) -> Nornir:
    """Initialize Nornir from a nornir config file."""
    return InitNornir(config_file=config_file)


def get_nornir() -> Nornir:
    """Get the initialized Nornir instance."""
    global _nornir_instance
    if _nornir_instance is None:
        raise RuntimeError(
            "Nornir not initialized. Use 'load_topology' or 'load_config' tools to "
            "initialize a fabric, or check if available topologies exist with 'list_topologies'."
        )
    return _nornir_instance


# ---- Report plumbing ----


def _parse_filters(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> tuple:
    """Parse comma-separated key=value filter strings into dicts."""

    def parse(spec: Optional[str]) -> Optional[Dict[str, str]]:
        if not spec:
            return None
        parsed: Dict[str, str] = {}
        for part in spec.split(","):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                parsed[key.strip()] = value.strip()
        return parsed

    return parse(inv_filter), parse(field_filter)


def _error_row(_node: str, exception: Optional[BaseException]) -> Dict[str, Any]:
    """Failed hosts are reported in-band, so an agent sees why a node is missing."""
    return {"_error": str(exception)}


def _query(spec: ReportSpec, inv_filter: Optional[Dict[str, str]], **params: Any):
    """Run a report's getter across the filtered inventory."""

    def task_func(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=spec.getter(device, **params))

    nornir = get_nornir()
    target = nornir.filter(**inv_filter) if inv_filter else nornir
    return target.run(task=task_func, name=spec.resource, raise_on_error=False)


def _run_report(
    name: str,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
    **params: Any,
) -> str:
    """Run a report from the registry and return its rows as JSON."""
    spec = get_report(name)
    i_filter, f_filter = _parse_filters(inv_filter, field_filter)
    result = _query(spec, i_filter, **params)
    _columns, per_node = extract(
        spec.resource, result, field_filter=f_filter, on_error=_error_row
    )
    rows = [
        {"Node": node.node, **row.values} for node in per_node for row in node.rows
    ]
    return json.dumps(
        [{clean_structured_key(k): v for k, v in row.items()} for row in rows],
        indent=2,
        default=str,
    )


# ---- MCP Server definition ----

mcp = FastMCP(
    "fcli",
    instructions=(
        "MCP server for Nokia SR Linux fabric analysis via fcli/nornir-srl. "
        "Provides tools to query operational state of SR Linux devices in a containerlab or production fabric. "
        "All tools return structured JSON data. "
        "INVENTORY FILTERS (inv_filter): inv_filter matches against node labels defined in the containerlab "
        "topology file. Only keys present in a node's 'labels:' section can be used as filter keys. "
        "For example, 'role=leaf' only works if the topology file defines 'labels: {role: leaf}' on nodes. "
        "If labels are absent or the key does not exist, inv_filter returns NO results. "
        "Use 'show_topology' first to see available nodes and their filterable label keys before applying inv_filter. "
        "If no labels are available, omit inv_filter to target all nodes. "
        "FIELD FILTERS (field_filter): use field_filter to filter output rows (e.g. 'session-state=established'). "
        "field_filter values are regex patterns matched case-insensitively against field values. "
        "inv_filter supports wildcards (*, ?). Both accept comma-separated key=value pairs. "
        "Topologies can be loaded at runtime using 'load_topology' or 'load_config'."
    ),
)


@mcp.tool()
def list_topologies(directory: str = ".") -> str:
    """List available containerlab topology files and nornir configs in a directory.

    Args:
        directory: Directory to search (default: current directory).
    """
    clab_patterns = [
        os.path.join(directory, "**/*.clab.yml"),
        os.path.join(directory, "**/*.clab.yaml"),
        os.path.join(directory, "**/clab-*.yml"),
        os.path.join(directory, "**/clab-*.yaml"),
    ]
    nornir_patterns = [
        os.path.join(directory, "**/nornir_config.yaml"),
        os.path.join(directory, "**/nornir_config*.yaml"),
    ]

    clab_files = set()
    for p in clab_patterns:
        for f in glob.glob(p, recursive=True):
            if os.path.isfile(f):
                clab_files.add(os.path.abspath(f))

    nornir_files = set()
    for p in nornir_patterns:
        for f in glob.glob(p, recursive=True):
            if os.path.isfile(f):
                nornir_files.add(os.path.abspath(f))

    results = {
        "containerlab_topologies": sorted(list(clab_files)),
        "nornir_configs": sorted(list(nornir_files)),
    }
    return json.dumps(results, indent=2)


@mcp.tool()
def load_topology(
    topo_file: str,
    cert_file: Optional[str] = None,
    inv_filter: Optional[str] = None,
    gnmi_port: int = SRL_DEFAULT_GNMI_PORT,
) -> str:
    """Initialize or switch the active fabric from a containerlab topology file.

    Args:
        topo_file: Path to the containerlab .yml file.
        cert_file: Optional path to the TLS certificate file.
        inv_filter: Optional inventory filter as comma-separated key=value pairs (e.g. 'role=leaf').
            Only keys defined in node 'labels:' in the topology file can be used.
            If labels are not defined on nodes, omit this parameter.
        gnmi_port: gNMI port for SR Linux nodes (default: 57400). EDA-deployed labs typically use 57410.
    """
    global _nornir_instance
    _nornir_instance = _init_nornir_from_topo(topo_file, cert_file, gnmi_port)

    all_label_keys: set = set()
    for host in _nornir_instance.inventory.hosts.values():
        if host.data:
            all_label_keys.update(host.data.keys())

    if inv_filter:
        i_filt, _ = _parse_filters(inv_filter=inv_filter)
        if i_filt:
            _nornir_instance = _nornir_instance.filter(**i_filt)

    label_info = (
        f" Available inv_filter keys (from node labels): {sorted(all_label_keys)}."
        if all_label_keys
        else " No labels defined on nodes; inv_filter will not match any nodes."
    )
    return (
        f"Fabric initialized from {topo_file}. "
        f"{len(_nornir_instance.inventory.hosts)} nodes matched filter.{label_info}"
    )


@mcp.tool()
def load_config(
    config_file: str,
    inv_filter: Optional[str] = None,
) -> str:
    """Initialize or switch the active fabric from a Nornir config file.

    Args:
        config_file: Path to the nornir_config.yaml file.
        inv_filter: Optional inventory filter as comma-separated key=value pairs.
            Matches against host data attributes. Use 'show_topology' to see available keys.
    """
    global _nornir_instance
    _nornir_instance = _init_nornir_from_config(config_file)

    if inv_filter:
        i_filt, _ = _parse_filters(inv_filter=inv_filter)
        if i_filt:
            _nornir_instance = _nornir_instance.filter(**i_filt)

    return f"Fabric initialized from {config_file}. {len(_nornir_instance.inventory.hosts)} nodes matched filter."


@mcp.tool()
def show_topology() -> str:
    """Show the currently loaded fabric: nodes, their labels, and available inv_filter keys.

    Use this to discover which inv_filter keys are available before filtering.
    Labels originate from the 'labels:' section of nodes in the containerlab topology file.
    Only label keys present here can be used in inv_filter; if no labels exist, omit inv_filter.
    """
    nornir = get_nornir()
    nodes = {}
    all_label_keys: set = set()
    for name, host in nornir.inventory.hosts.items():
        labels = dict(host.data) if host.data else {}
        nodes[name] = {"labels": labels}
        all_label_keys.update(labels.keys())
    return json.dumps(
        {
            "node_count": len(nodes),
            "available_inv_filter_keys": (
                sorted(all_label_keys) if all_label_keys else []
            ),
            "hint": (
                "Use these keys in inv_filter (e.g. 'role=leaf'). "
                "If available_inv_filter_keys is empty, no labels are defined and inv_filter will match nothing."
            ),
            "nodes": nodes,
        },
        indent=2,
    )


@mcp.tool()
def sys_info(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get system information for SR Linux nodes.

    Returns chassis type, serial number, hardware MAC, last boot time, software version, etc.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("sys_info", inv_filter, field_filter)


@mcp.tool()
def bgp_peers(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get BGP peer status and route statistics for all network instances.

    Returns peer address, peer AS, session state, local AS, flags (D=dynamic, B=BFD, F=fast-failover),
    and Rx/Active/Tx route counts per AFI/SAFI: IPv4 unicast (U4), IPv6 unicast (U6), EVPN,
    L3VPN IPv4 (VPNv4), L3VPN IPv6 (VPNv6). Table column headers show the AFI on the first line
    and R/A/T on the second; JSON keys use a space instead of the newline.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("bgp_peers", inv_filter, field_filter)


@mcp.tool()
def bgp_rib(
    route_fam: Literal[
        "evpn",
        "ipv4",
        "ipv6",
        "l3vpn-v4",
        "l3vpn-v6",
        "l3vpn-ipv4-unicast",
        "l3vpn-ipv6-unicast",
    ],
    route_type: Optional[Literal["1", "2", "3", "4", "5"]] = None,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get BGP RIB (Routing Information Base) entries.

    Returns the full set of path attributes for each route, including standard
    communities, Site-of-Origin (soo), BGP domain-path (dpath), tunnel-encap
    extended-community, route-target (RT), as-path, next-hop, and route status
    (valid/best/used, tie-break reason, internal-tags). Useful for diagnosing
    EVPN/IP-VPN loop-prevention and route-leaking issues.

    Args:
        route_fam: BGP RIB address family: evpn, ipv4, ipv6, or L3VPN IPv4/IPv6 unicast
            (``l3vpn-v4`` / ``l3vpn-v6`` short names, or ``l3vpn-ipv4-unicast`` / ``l3vpn-ipv6-unicast``).
        route_type: Route type for EVPN (1-5). Only applicable when route_fam='evpn'.
            1=Ethernet Auto-Discovery, 2=MAC/IP, 3=Inclusive Multicast, 4=ES, 5=IP Prefix.
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report(
        "bgp_rib",
        inv_filter,
        field_filter,
        route_fam=route_fam,
        route_type=route_type,
        detail=True,
    )


@mcp.tool()
def ipv4_rib(
    address: Optional[str] = None,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IPv4 routing table entries.

    Shows active routes with next-hops, metrics, preferences, and route owners.

    Args:
        address: Optional IP address for longest-prefix-match (LPM) lookup (e.g. '10.0.0.1').
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("ipv4_rib", inv_filter, field_filter, address=address)


@mcp.tool()
def ipv6_rib(
    address: Optional[str] = None,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IPv6 routing table entries.

    Shows active routes with next-hops, metrics, preferences, and route owners.

    Args:
        address: Optional IPv6 address for longest-prefix-match (LPM) lookup.
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("ipv6_rib", inv_filter, field_filter, address=address)


@mcp.tool()
def static_routes(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get static routes from /network-instance[name=*]/static-routes.

    Returns route, admin-state, installed, metric, pref, and nhops.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("static_routes", inv_filter, field_filter)


@mcp.tool()
def tunnel_table(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get the IP tunnel-table from /network-instance[name=*]/tunnel-table.

    Returns transport tunnels (LDP, SR-ISIS, RSVP, VXLAN, ...) with the
    resolved egress interface, next-hop IP, pushed MPLS label-stack, tunnel
    type, owner, metric and preference. Useful for verifying which transport
    a remote endpoint (e.g. a loopback) is reached over, and on which port.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("tunnel_table", inv_filter, field_filter)


@mcp.tool()
def network_instances(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get network instances and their interfaces.

    Returns NI name, operational state, type (ip-vrf/mac-vrf/default), router-id,
    vxlan-interface, import/export RTs, and associated interfaces with IP addresses, VLANs, and MTU.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("ni", inv_filter, field_filter)


@mcp.tool()
def subinterfaces(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get sub-interfaces of SR Linux nodes.

    Returns interface name, sub-interface index, type (routed/bridged), admin/oper state,
    IPv4/IPv6 addresses, VLAN ID, and more.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("subif", inv_filter, field_filter)


@mcp.tool()
def lag(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get LAG (Link Aggregation Group) information.

    Returns LAG name, oper state, MTU, min-links, LACP config, and member interfaces.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("lag", inv_filter, field_filter)


@mcp.tool()
def ifstats(
    interval: int = 5,
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get per-interface traffic rates (in/out bps) computed from two consecutive gNMI samples.

    Queries interface statistics twice with a configurable interval, then calculates
    the delta to derive rates for each interface.

    Returns per interface: in-Kbps/out-Kbps (rate over the interval), in-err/out-err
    and in-disc/out-disc (deltas), plus cumulative counters in-pkts/out-pkts and
    in-octets/out-octets. Idle interfaces are included so raw counters are always
    available.

    Args:
        interval: Seconds between the two samples (default 5).
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("ifstats", inv_filter, field_filter, interval=interval)


@mcp.tool()
def mac_table(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get MAC address table entries.

    Returns network instance, MAC address, destination (interface or VXLAN), and type (learnt/static/evpn).

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("mac", inv_filter, field_filter)


@mcp.tool()
def irb_interfaces(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IRB (Integrated Routing and Bridging) sub-interface details.

    Returns IRB subinterface, network instance, IPv4/IPv6 addresses, anycast gateway config,
    ARP/ND settings, EVPN advertising config, and ILR (Interface-Less Routing) support.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("irb", inv_filter, field_filter)


@mcp.tool()
def ethernet_segments(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get EVPN Ethernet Segment information.

    Returns ESI, type, multi-homing mode, oper state, interfaces, next-hops,
    and associated network instances with DF (Designated Forwarder) candidates.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("es", inv_filter, field_filter)


@mcp.tool()
def es_destinations(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get Ethernet Segment destinations from bridge tables.

    Returns tunnel name, ESI, and VTEP destinations. VTEP destinations are comma-separated list of (vtep-address, vni) and are dynamically generated, based on traffic/routing data.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("es_dest", inv_filter, field_filter)


@mcp.tool()
def vxlan_tunnels(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get VXLAN tunnel interfaces and unicast destinations.

    Returns VXLAN interface name, associated network instance, ingress-vni,
    and unicast destinations (vtep-address, vni).

    Note: unicast destinations are populated by receipt of EVPN type-2 (MAC/IP) routes,
    which typically requires actual traffic. If no destinations are shown, it means no
    type-2 routes have been received for that VNI yet.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("vxlan", inv_filter, field_filter)


@mcp.tool()
def lldp_neighbors(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get LLDP neighbor information.

    Returns local interface, neighbor system name, and neighbor port ID/description.
    Useful for understanding physical topology and connectivity.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("lldp", inv_filter, field_filter)


@mcp.tool()
def arp_table(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get ARP table entries.

    Returns interface, network instance, IPv4 address, MAC address, type, and expiry time.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("arp", inv_filter, field_filter)


@mcp.tool()
def ipv6_neighbors(
    inv_filter: Optional[str] = None,
    field_filter: Optional[str] = None,
) -> str:
    """Get IPv6 Neighbor Discovery table entries.

    Returns interface, IPv6 address, MAC address, state, type, and next state time.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
        field_filter: Field filter as comma-separated key=value pairs to filter output rows
            (e.g. 'session-state=established'). Values are case-insensitive regexes.
    """
    return _run_report("nd", inv_filter, field_filter)


@mcp.tool()
def routing_policies(
    inv_filter: Optional[str] = None,
) -> str:
    """Get routing policies configured on the node.

    Returns the structured JSON response from /routing-policy.

    Args:
        inv_filter: Inventory filter as comma-separated key=value pairs (e.g. 'role=leaf,site=dc1').
            Supports wildcards. Matches against node labels from the topology file; use
            'show_topology' to see available keys. Omit to target all nodes.
    """
    spec = get_report("routing_pol")
    i_filter, _ = _parse_filters(inv_filter, None)
    result = _query(spec, i_filter)

    policies: List[Dict[str, Any]] = []
    for host, host_result in result.items():
        r = host_result[0]
        node = r.host.hostname if r.host and r.host.hostname else host
        if r.failed:
            policies.append({"Node": node, "_error": str(r.exception)})
            continue
        for policy in (r.result or {}).get(spec.resource) or []:
            policies.append({"Node": node, "routing-policy": policy})

    return json.dumps(policies, indent=2, default=str)


# ---- CLI entry point ----


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP Server for fcli - SR Linux fabric analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  fcli-mcp --topo-file topo.yml                  # stdio transport with clab topology\n"
            "  fcli-mcp --topo-file topo.yml --transport http  # HTTP/SSE transport\n"
            "  fcli-mcp --config-file nornir_config.yaml       # Use nornir config file\n"
        ),
    )
    parser.add_argument(
        "--topo-file",
        "-t",
        help="Containerlab topology file (mutually exclusive with --config-file)",
    )
    parser.add_argument(
        "--config-file",
        "-c",
        help="Nornir config file (mutually exclusive with --topo-file)",
    )
    parser.add_argument(
        "--cert-file",
        help="TLS certificate file for containerlab",
    )
    parser.add_argument(
        "--inv-filter",
        "-i",
        action="append",
        help="Inventory filter in key=value format (can be repeated)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport type (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind HTTP server to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for HTTP server (default: 8080)",
    )

    args = parser.parse_args()

    if args.topo_file and args.config_file:
        parser.error("--topo-file and --config-file are mutually exclusive")

    # Initialize Nornir
    global _nornir_instance
    try:
        if args.topo_file:
            _nornir_instance = _init_nornir_from_topo(args.topo_file, args.cert_file)
        elif args.config_file:
            _nornir_instance = _init_nornir_from_config(args.config_file)
        elif os.path.exists("nornir_config.yaml"):
            _nornir_instance = _init_nornir_from_config("nornir_config.yaml")
    except Exception as e:
        logger.error("Failed to initialize Nornir: %s", e)

    # Apply global inventory filter if provided
    if _nornir_instance and args.inv_filter:
        i_filter = {}
        for f in args.inv_filter:
            if "=" in f:
                k, v = f.split("=", 1)
                i_filter[k] = v
        if i_filter:
            _nornir_instance = _nornir_instance.filter(**i_filter)

    # Run server
    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
