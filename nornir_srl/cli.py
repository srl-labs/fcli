import csv
import io
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from enum import Enum
import logging
import os
import time

import typer
import yaml  # type: ignore
from rich.console import Console
from rich.table import Table
from rich.box import MINIMAL_DOUBLE_HEAD
from rich.theme import Theme
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.task import Result, Task, AggregatedResult

from . import clab
from .connections.srlinux import CONNECTION_NAME
from .connections.routing import BGP_RIB_ROUTE_FAM_ALIASES
from .connections.helpers import clean_structured_key
from .reports import ReportSpec, get_report
from .rows import NodeRows, clean_columns, extract
from .utils.logging_config import setup_logging
from . import __version__


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class OutputFormat(str, Enum):
    TABLE = "table"
    JSON = "json"
    YAML = "yaml"
    CSV = "csv"


def _version_callback(value: bool):
    if value:
        typer.echo(__version__)
        raise typer.Exit()


app = typer.Typer(name="fcli", help="Nornir SRLinux CLI")
logger = logging.getLogger(__name__)


SRL_DEFAULT_GNMI_PORT = clab.SRL_DEFAULT_GNMI_PORT
NORNIR_DEFAULT_CONFIG = clab.NORNIR_DEFAULT_CONFIG


# ------------------------- helpers -------------------------


def _report_failure(resource: str) -> Callable[[str, Optional[BaseException]], None]:
    """Report a host that failed, and leave it out of the table."""

    def on_error(node: str, exception: Optional[BaseException]) -> None:
        typer.echo(f"Failed to get {resource} for {node}. Exception: {exception}")

    return on_error


def print_structured(
    col_names: List[str],
    rows: List[Dict[str, Any]],
    output_format: OutputFormat,
) -> None:
    """Print data in JSON, YAML, or CSV format."""
    if not rows:
        typer.echo("No data...")
        return

    col_names = clean_columns(col_names)
    rows = [{clean_structured_key(k): v for k, v in row.items()} for row in rows]

    all_cols = ["Node"] + col_names

    if output_format == OutputFormat.JSON:
        typer.echo(json.dumps(rows, indent=2, default=str))
    elif output_format == OutputFormat.YAML:
        typer.echo(yaml.safe_dump(rows, default_flow_style=False).rstrip())
    elif output_format == OutputFormat.CSV:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=all_cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: str(v) for k, v in row.items()})
        typer.echo(buf.getvalue().rstrip())


TABLE_THEME = Theme(
    {"ok": "green", "warn": "orange3", "info": "blue", "err": "bold red"}
)

#: Values worth colouring wherever they turn up in a table.
STYLE_MAP = {
    "up": "[ok]",
    "down": "[err]",
    "enable": "[ok]",
    "disable": "[info]",
    "routed": "[cyan]",
    "bridged": "[blue]",
    "established": "[ok]",
    "active": "[cyan]",
}


def _box(box_type: Optional[str]) -> Any:
    if not box_type:
        return MINIMAL_DOUBLE_HEAD
    name = str(box_type).upper()
    try:
        return getattr(__import__("rich.box", fromlist=["box"]), name)
    except AttributeError:
        typer.echo(
            f"Unknown box type {name}. Check 'python -m rich.box' for valid box types."
        )
        return MINIMAL_DOUBLE_HEAD


def _cell(value: Any) -> str:
    text = str(value)
    return STYLE_MAP.get(text, "") + text


def print_table(
    title: str,
    columns: List[str],
    per_node: List[NodeRows],
    *,
    box_type: Optional[str] = None,
) -> None:
    """Render the extracted rows as a rich table, one section per node."""
    console = Console(theme=TABLE_THEME)
    console._emoji = False
    table = Table(title=title, highlight=True, box=_box(box_type))
    table.add_column("Node", no_wrap=True)
    for col in columns:
        table.add_column(col, no_wrap=False)

    for node in per_node:
        first = True
        for row in node.rows:
            # Fields a row inherited from its parent item are shown once, so a
            # parent with many sub-rows reads as one entry spanning them.
            cells = row.cells(group=True)
            values = [_cell(cells.get(col, "")) for col in columns]
            table.add_row(node.node if first else "", *values)
            first = False
        table.add_section()

    if len(table.columns) > 1:
        console.print(table)
    else:
        console.print("[i]No data...[/i]")


def print_report(
    result: AggregatedResult,
    name: str,
    failed_hosts: List[str],
    box_type: Optional[str] = None,
    f_filter: Optional[Dict] = None,
    i_filter: Optional[Dict] = None,
    output: OutputFormat = OutputFormat.TABLE,
) -> None:
    columns, per_node = extract(
        result.name,
        result,
        field_filter=f_filter,
        on_error=_report_failure(result.name),
    )
    if output == OutputFormat.TABLE:
        title = "[bold]" + name + "[/bold]"
        if f_filter:
            title += "\nFields filter:" + str(f_filter)
        if i_filter:
            title += "\nInventory filter:" + str(i_filter)
        if len(failed_hosts) > 0:
            title += "\n[red]Failed hosts:" + str(failed_hosts)
        if not columns:
            logger.debug("No data returned for %s: %s", result.name, result)
        print_table(title, columns, per_node, box_type=box_type)
    else:
        rows = [
            {"Node": node.node, **row.values} for node in per_node for row in node.rows
        ]
        print_structured(columns, rows, output)


# ------------------------- root callback -------------------------


@app.callback()
def main(
    ctx: typer.Context,
    cfg: Optional[Path] = typer.Option(
        None,
        "--cfg",
        "-c",
        help="Nornir config file. Mutually exclusive with -t. Defaults to nornir_config.yaml",
    ),
    inv_filter: Optional[List[str]] = typer.Option(
        None,
        "--inv-filter",
        "-i",
        help="Inventory filter in key=value format. Can be provided multiple times",
    ),
    box_type: Optional[str] = typer.Option(
        None,
        "--box-type",
        "-b",
        help="Box type of printed table, e.g. -b minimal_double_head. 'python -m rich.box' for options",
    ),
    topo_file: Optional[Path] = typer.Option(
        None,
        "--topo-file",
        "-t",
        exists=True,
        help="CLAB topology file, mutually exclusive with -c",
    ),
    cert_file: Optional[Path] = typer.Option(
        None, "--cert-file", exists=True, help="CLAB certificate file"
    ),
    gnmi_port: int = typer.Option(
        SRL_DEFAULT_GNMI_PORT,
        "--gnmi-port",
        "-p",
        help="gNMI port for SR Linux nodes (default: 57400)",
    ),
    log_level: LogLevel = typer.Option(
        LogLevel.ERROR, "--log-level", "-l", help="Set logging level"
    ),
    log_file: Optional[Path] = typer.Option(
        None, "--log-file", "-f", help="Optional log file"
    ),
    output: OutputFormat = typer.Option(
        OutputFormat.TABLE,
        "--output",
        "-o",
        help="Output format: table, json, yaml, csv",
        case_sensitive=False,
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
) -> None:
    setup_logging(log_level.value, str(log_file) if log_file else None)
    ctx.ensure_object(dict)
    if topo_file:
        try:
            with open(topo_file, "r") as f:
                topo = yaml.safe_load(os.path.expandvars(f.read()))
        except Exception as e:
            typer.echo(f"Failed to load topology file {topo_file}: {e}")
            raise typer.Exit(1)
        lab_name = topo["name"]
        hosts = clab.srl_hosts(topo)
        logger.debug(
            "topology '%s' from %s holds %d SR Linux node(s): %s",
            lab_name,
            topo_file,
            len(hosts),
            ", ".join(sorted(hosts)),
        )
        groups = clab.srl_groups(gnmi_port, str(cert_file) if cert_file else None)
        with tempfile.NamedTemporaryFile("w+") as hosts_f:
            yaml.safe_dump(hosts, hosts_f)
            hosts_f.seek(0)
            with tempfile.NamedTemporaryFile("w+") as groups_f:
                yaml.safe_dump(groups, groups_f)
                groups_f.seek(0)
                conf: Dict[str, Any] = NORNIR_DEFAULT_CONFIG
                conf.update(
                    {
                        "inventory": {
                            "options": {
                                "host_file": hosts_f.name,
                                "group_file": groups_f.name,
                            }
                        }
                    }
                )
                fabric = InitNornir(**conf)
    else:
        if cfg is None:
            cfg = Path("nornir_config.yaml")
        if not cfg.exists():
            typer.echo(
                f"Config file '{cfg}' does not exist. Provide -c/--cfg or -t/--topo-file."
            )
            raise typer.Exit(1)
        logger.debug("initializing Nornir from %s", cfg)
        fabric = InitNornir(config_file=str(cfg))

    i_filter = (
        {k: v for k, v in (f.split("=") for f in inv_filter)} if inv_filter else {}
    )
    target: Nornir = fabric.filter(**i_filter) if i_filter else fabric
    logger.debug(
        "inventory holds %d node(s), %d selected by filter %s: %s",
        len(fabric.inventory.hosts),
        len(target.inventory.hosts),
        i_filter or "-",
        ", ".join(sorted(target.inventory.hosts)),
    )
    ctx.obj["target"] = target
    ctx.obj["i_filter"] = i_filter
    ctx.obj["box_type"] = box_type.upper() if box_type else None
    ctx.obj["output"] = output
    ctx.obj["log_level"] = log_level.value
    ctx.obj["topo_name"] = lab_name if topo_file else None


# ------------------------- command helpers -------------------------


def _task_for(spec: ReportSpec, params: Dict[str, Any]) -> Callable[[Task], Result]:
    """Wrap a report's getter as a Nornir task."""

    def task_func(task: Task) -> Result:
        device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
        return Result(host=task.host, result=spec.getter(device, **params))

    return task_func


def run_query(
    ctx: typer.Context, spec: ReportSpec, **params: Any
) -> AggregatedResult:
    """Run a report's getter across the filtered inventory."""
    target: Nornir = ctx.obj["target"]
    logger.debug(
        "running report '%s' (resource '%s') on %d node(s) with params %s",
        spec.name,
        spec.resource,
        len(target.inventory.hosts),
        params or "-",
    )
    started = time.perf_counter()
    result = target.run(
        task=_task_for(spec, params), name=spec.resource, raise_on_error=False
    )
    logger.debug(
        "report '%s' finished in %.3fs, %d/%d node(s) failed: %s",
        spec.name,
        time.perf_counter() - started,
        len(result.failed_hosts),
        len(result),
        ", ".join(sorted(result.failed_hosts)) or "none",
    )
    logger.debug("Aggregated result for %s: %s", spec.name, result)
    return result


def run_report(
    ctx: typer.Context,
    name: str,
    field_filter: Optional[List[str]] = None,
    title: Optional[str] = None,
    **params: Any,
) -> None:
    """Run the named report from the registry and print it."""
    spec = get_report(name)
    f_filter = (
        {k: v for k, v in (f.split("=") for f in field_filter)} if field_filter else {}
    )
    result = run_query(ctx, spec, **params)
    print_report(
        result=result,
        name=title or spec.title,
        failed_hosts=result.failed_hosts,
        box_type=ctx.obj["box_type"],
        f_filter=f_filter,
        i_filter=ctx.obj["i_filter"],
        output=ctx.obj["output"],
    )


# ------------------------- commands -------------------------


@app.command()
def server(
    ctx: typer.Context,
    listen: str = typer.Option(
        "127.0.0.1",
        "--listen",
        "-L",
        help="Address to bind the web server to. Use 0.0.0.0 to expose it on all interfaces",
    ),
    port: int = typer.Option(8080, "--port", "-P", help="TCP port to listen on"),
    sample_interval: Optional[int] = typer.Option(
        None,
        "--sample-interval",
        "-S",
        help="Override the gNMI SAMPLE interval (seconds) of every subscription",
    ),
    refresh: float = typer.Option(
        2.0,
        "--refresh",
        "-R",
        help="How often (seconds) a table is re-rendered and pushed to the browser",
    ),
    resync: int = typer.Option(
        300,
        "--resync",
        help="Interval (seconds) for a full gNMI re-read per node; 0 disables it",
    ),
    idle_timeout: int = typer.Option(
        900,
        "--idle-timeout",
        help="Stop streaming paths no report has read for this long (seconds); "
        "0 keeps every path subscribed for the lifetime of the server",
    ),
) -> None:
    """Serves live report tables over HTTP, fed by gNMI subscriptions"""
    from .server.app import serve

    target: Nornir = ctx.obj["target"]
    if not target.inventory.hosts:
        typer.echo("No hosts in the inventory. Check your -c/-t and -i options.")
        raise typer.Exit(1)
    typer.echo(
        f"fcli server on http://{listen}:{port} "
        f"({len(target.inventory.hosts)} node(s))"
    )
    serve(
        target,
        host=listen,
        port=port,
        sample_interval=sample_interval,
        resync_interval=resync,
        refresh=refresh,
        idle_timeout=idle_timeout,
        log_level=ctx.obj["log_level"],
        topo_name=ctx.obj.get("topo_name"),
    )


FIELD_FILTER = typer.Option(
    None,
    "--field-filter",
    "-f",
    help="Filter rows on field values, e.g. -f oper-state=down. Values are "
    "case-insensitive regexes; repeat the option to filter on several fields",
)


@app.command()
def sys_info(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays System Info of nodes"""
    run_report(ctx, "sys_info", field_filter)


@app.command()
def bgp_peers(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays BGP Peers and their status"""
    run_report(ctx, "bgp_peers", field_filter)


@app.command()
def bgp_rib(
    ctx: typer.Context,
    route_fam: str = typer.Option(
        ...,
        "--route-fam",
        "-r",
        help="evpn | ipv4 | ipv6 | l3vpn-v4 | l3vpn-v6 (IP-VPN unicast; full names "
        "l3vpn-ipv4-unicast / l3vpn-ipv6-unicast also accepted)",
        case_sensitive=False,
    ),
    route_type: Optional[str] = typer.Option(
        None, "--route-type", "-t", help="Route type for EVPN"
    ),
    detail: bool = typer.Option(
        False,
        "--detail",
        "-d",
        help="Include all path attributes (communities, SoO, D-PATH, tunnel-encap, "
        "status). Automatically enabled for non-table output (json/yaml/csv).",
    ),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays BGP RIB"""
    family = BGP_RIB_ROUTE_FAM_ALIASES.get(route_fam.lower(), route_fam)
    run_report(
        ctx,
        "bgp_rib",
        field_filter,
        title=f"BGP RIB ({family})",
        route_fam=route_fam,
        route_type=route_type,
        # Structured output has room for every attribute, so always include them.
        detail=detail or ctx.obj["output"] != OutputFormat.TABLE,
    )


@app.command()
def ipv4_rib(
    ctx: typer.Context,
    address: Optional[str] = typer.Option(
        None,
        "--address",
        "-a",
        help="Look up specified address in the IPv4 RIB using LPM",
    ),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IPv4 RIB entries"""
    run_report(ctx, "ipv4_rib", field_filter, address=address)


@app.command()
def ipv6_rib(
    ctx: typer.Context,
    address: Optional[str] = typer.Option(
        None,
        "--address",
        "-a",
        help="Look up specified address in the IPv6 RIB using LPM",
    ),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IPv6 RIB entries"""
    run_report(ctx, "ipv6_rib", field_filter, address=address)


@app.command()
def static_routes(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays static routes"""
    run_report(ctx, "static_routes", field_filter)


@app.command()
def tunnel_table(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays the IP tunnel-table (LDP, SR-ISIS, RSVP, VXLAN, ...)"""
    run_report(ctx, "tunnel_table", field_filter)


@app.command()
def ni(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays Network Instances and interfaces"""
    run_report(ctx, "ni", field_filter)


@app.command()
def subif(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays Sub-Interfaces of nodes"""
    run_report(ctx, "subif", field_filter)


@app.command()
def lag(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays LAGs of nodes"""
    run_report(ctx, "lag", field_filter)


@app.command()
def ifstats(
    ctx: typer.Context,
    interval: int = typer.Option(5, "--interval", "-s", help="Seconds between samples"),
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays per-interface in/out bps from two consecutive samples"""
    run_report(
        ctx,
        "ifstats",
        field_filter,
        title=f"Interface Stats ({interval}s interval)",
        interval=interval,
    )


@app.command()
def mac(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays MAC Table"""
    run_report(ctx, "mac", field_filter)


@app.command()
def irb(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IRB sub-interfaces"""
    run_report(ctx, "irb", field_filter)


@app.command()
def es(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays Ethernet Segments"""
    run_report(ctx, "es", field_filter)


@app.command()
def es_dest(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays ES Destinations on the bridge table"""
    run_report(ctx, "es_dest", field_filter)


@app.command()
def vxlan(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays VXLAN tunnel interfaces and unicast destinations"""
    run_report(ctx, "vxlan", field_filter)


@app.command()
def lldp(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays LLDP Neighbors"""
    run_report(ctx, "lldp", field_filter)


@app.command()
def arp(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays ARP table"""
    run_report(ctx, "arp", field_filter)


@app.command()
def nd(
    ctx: typer.Context,
    field_filter: Optional[List[str]] = FIELD_FILTER,
) -> None:
    """Displays IPv6 Neighbors"""
    run_report(ctx, "nd", field_filter)


@app.command()
def routing_pol(ctx: typer.Context) -> None:
    """Displays Routing Policies"""
    spec = get_report("routing_pol")
    # Policies nest arbitrarily deep, so there is no table to render them as.
    if ctx.obj["output"] not in (OutputFormat.JSON, OutputFormat.YAML):
        typer.echo(
            f"Warning: the {spec.name.replace('_', '-')} report only supports json "
            "or yaml output.",
            err=True,
        )
        raise typer.Exit(1)

    result = run_query(ctx, spec)
    policies: List[Dict[str, Any]] = []
    for host, host_result in result.items():
        r: Result = host_result[0]
        node = r.host.hostname if r.host and r.host.hostname else host
        if r.failed:
            typer.echo(
                f"Failed to get {spec.resource} for {host}. Exception: {r.exception}",
                err=True,
            )
            continue
        for policy in (r.result or {}).get(spec.resource) or []:
            policies.append({"Node": node, "routing-policy": policy})

    if not policies:
        typer.echo("No data...")
        return
    if ctx.obj["output"] == OutputFormat.JSON:
        typer.echo(json.dumps(policies, indent=2, default=str))
    else:
        typer.echo(yaml.safe_dump(policies, default_flow_style=False).rstrip())


if __name__ == "__main__":
    app()
