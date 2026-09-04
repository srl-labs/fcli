# fcli

[![ci](https://github.com/srl-labs/fcli/actions/workflows/ci.yml/badge.svg)](https://github.com/srl-labs/fcli/actions/workflows/ci.yml)
[![SR Linux](https://img.shields.io/badge/SR%20Linux-25.3.2%20%7C%2025.10.3%20%7C%2026.3.1%20%7C%2026.7.1-blue)](#tested-sr-linux-releases)
[![PyPI](https://img.shields.io/pypi/v/nornir-srl)](https://pypi.org/project/nornir-srl/)

**fcli** is a fabric observability tool for Nokia SR Linux. It talks gNMI to every node in the inventory and presents the same reports on three surfaces:

1. **`fcli server`** — a live web UI, kept current by gNMI subscriptions (the primary way to use it)
2. **`fcli <report>`** — one-shot CLI tables, JSON, YAML or CSV
3. **`fcli-mcp`** — the same reports as MCP tools for AI agents

Inventory comes from a [containerlab](https://containerlab.dev/) topology file or a [Nornir](https://nornir.readthedocs.io/en/latest/) config. Report getters hide YANG and path differences across SR Linux releases, so a column means the same thing on 25.3 as on 26.7.

## Table of contents

- [Quick start](#quick-start)
- [Installation](#installation)
- [Inventory](#inventory)
- [Live server](#live-server)
  - [In the browser](#in-the-browser)
  - [Topology](#topology)
  - [Ask (LLM troubleshooting)](#ask-llm-troubleshooting)
  - [How the live data works](#how-the-live-data-works)
  - [gNMI sessions](#gnmi-sessions)
  - [HTTP API](#http-api)
- [CLI reports](#cli-reports)
  - [Examples](#examples)
  - [Debug logging](#debug-logging)
- [MCP server](#mcp-server)
- [Reports](#reports)
- [Tested SR Linux releases](#tested-sr-linux-releases)

## Quick start

With a running containerlab topology that has SR Linux nodes:

```bash
# install (once)
uv tool install git+https://github.com/srl-labs/fcli

# live UI — open http://127.0.0.1:8080
fcli -t topo.clab.yml server
```

That is the usual workflow. The CLI and MCP surfaces share the same inventory flags (`-t` / `-c`, `-i`) and the same report getters.

```bash
fcli -t topo.clab.yml bgp-peers
fcli -t topo.clab.yml -i role=leaf mac -f NI=macvrf-202
fcli-mcp --topo-file topo.clab.yml
```

## Installation

Requires Python 3.10+.

### `uv` (recommended)

[`uv`](https://github.com/astral-sh/uv) is a standalone Python package manager:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/srl-labs/fcli
```

This puts `fcli` and `fcli-mcp` on your `PATH` (typically `~/.local/bin`).

From a clone of this repo, `uv tool install .` or `uv pip install -e .` does the same against local sources.

### pip

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U nornir-srl
```

### Docker

The image is [ghcr.io/srl-labs/fcli](https://github.com/srl-labs/fcli/pkgs/container/fcli). Attach it to the containerlab management network, publish port 8080, and bind-mount the topology file:

```bash
CLAB_TOPO=topo.clab.yml
NET=$(grep '^name:' "$CLAB_TOPO" | awk '{print $2}')
alias fcli="docker run -it --network $NET --rm \
  -p 8080:8080 \
  -v /etc/hosts:/etc/hosts:ro \
  -v ${PWD}/${CLAB_TOPO}:/topo.yml \
  ghcr.io/srl-labs/fcli:latest -t /topo.yml"

fcli server --listen 0.0.0.0
fcli bgp-peers
```

`--listen 0.0.0.0` is required inside Docker so the published port is reachable from the host. Running the container with no arguments drops into a shell.

If the container cannot reach the lab from Docker's default `docker0` bridge, either attach it to the containerlab network as above, or allow inter-network traffic (blocked by default on some hosts):

```bash
iptables -I DOCKER-USER -o docker0 -j ACCEPT -m comment --comment "allow inter-network comms"
```

If the topology overrides the management network with `.mgmt.network`, use that name for `--network` instead of the lab name.

## Inventory

`fcli` needs a set of SR Linux nodes. Two ways to provide them, mutually exclusive:

| Flag | Source | Typical use |
| --- | --- | --- |
| `-t` / `--topo-file` | containerlab topology | labs |
| `-c` / `--cfg` | Nornir config file | hardware fabrics |

An inventory filter (`-i key=value`, repeatable, ANDed) scopes every surface to a subset of those nodes.

### Containerlab topology

```bash
fcli -t topo.clab.yml server
fcli -t topo.clab.yml -i role=leaf bgp-peers
```

Only nodes of kind `srl` / `nokia_srlinux` (or whose image is SR Linux) are inventoried. The topology `prefix` is applied to hostnames the same way containerlab does. Node `labels:` become host data and are the keys `-i` can filter on. TLS uses the lab's CA when `--cert-file` is given; the default gNMI port is 57400 (`--gnmi-port` / `-p` to override, e.g. 57410 for EDA-deployed labs).

### Nornir config

```bash
fcli -c nornir_config.yaml server
fcli -c nornir_config.yaml -i role=leaf mac
```

If neither `-t` nor `-c` is given, `nornir_config.yaml` in the current directory is used. A typical layout:

```yaml
# nornir_config.yaml
inventory:
    plugin: YAMLInventory
    options:
        host_file: "./inventory/hosts.yaml"
        group_file: "./inventory/groups.yaml"
        defaults_file: "./inventory/defaults.yaml"
runner:
    plugin: threaded
    options:
        num_workers: 20
```

```yaml
# inventory/hosts.yaml
leaf1:
    hostname: 192.0.2.11
    groups: [srl, leafs]
spine1:
    hostname: 192.0.2.21
    groups: [srl, spines]
```

```yaml
# inventory/groups.yaml
srl:
    connection_options:
        srlinux:
            port: 57400
            username: admin
            password: NokiaSrl1!
            extras:
                path_cert: "./root-ca.pem"
spines:
    data:
        role: spine
leafs:
    data:
        role: leaf
```

The certificate is specified once for the `srl` group via `connection_options.srlinux.extras.path_cert`. Host `data:` keys are what `-i` filters on.

## Live server

`fcli server` is the main interface. It serves the same reports as the CLI as a web UI, kept up to date by gNMI **subscriptions** instead of one-shot polls. Every node in the inventory gets a single `Subscribe` RPC carrying the paths the opened reports need, and the browser is pushed a new table over server-sent events whenever the data actually changes.

The tables are the CLI reports, rendered by the same getters, so a column in the browser means what it means in `fcli`. Reports that need arguments on the CLI are pre-bound for streaming: `bgp-rib` is split into one report per address family (and per EVPN route type). `routing-pol` is nested JSON rather than a table, so it is not served. The server also has reports the CLI does not: an **Overview** dashboard, a live **Topology** drawing, and EVPN **Services** / **Bridge Domains** / **Routers** views.

```
❯ fcli -t topo.clab.yml server
fcli server on http://127.0.0.1:8080 (6 node(s))
```

The global options are the same as for the CLI, so the server can be pointed at a containerlab topology (`-t`), a Nornir config (`-c`) and scoped to a subset of the fabric (`-i`):

```
❯ fcli -c nornir_config.yaml -i role=leaf server --listen 0.0.0.0 --port 8080
```

```
❯ fcli -t topo.clab.yml server --help

 Usage: fcli server [OPTIONS]

 Serves live report tables over HTTP, fed by gNMI subscriptions

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --listen           -L      TEXT     Address to bind the web server to. Use   │
│                                     0.0.0.0 to expose it on all interfaces   │
│                                     [default: 127.0.0.1]                     │
│ --port             -P      INTEGER  TCP port to listen on [default: 8080]    │
│ --sample-interval  -S      INTEGER  Override the gNMI SAMPLE interval        │
│                                     (seconds) of every subscription          │
│                                     [default: None]                          │
│ --refresh          -R      FLOAT    How often (seconds) a table is           │
│                                     re-rendered and pushed to the browser    │
│                                     [default: 2.0]                           │
│ --resync                   INTEGER  Interval (seconds) for a full gNMI       │
│                                     re-read per node; 0 disables it          │
│                                     [default: 300]                           │
│ --idle-timeout             INTEGER  Stop streaming paths no report has read  │
│                                     for this long (seconds); 0 keeps every   │
│                                     path subscribed for the lifetime of the  │
│                                     server                                   │
│                                     [default: 900]                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The server binds to localhost by default. It has no authentication of its own, so put it behind a reverse proxy (or keep it on localhost) before exposing it with `--listen 0.0.0.0`.

### In the browser

* **Reports** are listed in the sidebar, grouped by category, with a filter box on top. The selected report is kept in the URL fragment (`http://localhost:8080/#bgp_peers`), so a view can be bookmarked or shared.
* **Overview** is a KPI dashboard: node connectivity, interface and BGP-session health, derived from the same streamed trees as the tables.
* **Sorting**: click a column header to sort, click again to reverse. Sorting is natural, so `ethernet-1/10` comes after `ethernet-1/2`.
* **Filtering**: each column has its own filter box, and the search box above the table filters on all visible columns at once. Both accept a regular expression and fall back to a substring match if the expression is not valid (yet).
* **Inventory filter**: the same `key=value` filter as the CLI's `-i`, applied live — e.g. `role=leaf`.
* **Live updates**: cells that changed since the previous update flash, and new rows flash as a whole. **Pause** freezes the table without dropping the subscriptions.
* **Columns** hides columns you do not need, **CSV** downloads exactly what the table currently shows (filters, column selection and all).
* **Nodes** in the sidebar show per-node subscription state (`up` / unreachable / pending). Clicking a node jumps to details.
* **Theme** toggles light and dark.

### Topology

The **Topology** page draws the fabric from LLDP, one tier per row, and works out what each node is from what it runs rather than from an inventory label:

* a node with **mac-vrfs**, and optionally ip-vrfs, is a **leaf** — the tier where a service meets a port;
* a node whose services carry **two enabled `bgp-vpn` instances** is a **DCGW**: the second instance is the WAN side of a stitched service, which only a gateway out of the DC has;
* a node with **no mac-vrf and no ip-vrf** that sees two or more leaves is a **spine**: it interconnects them without terminating anything;
* anything else without services is **WAN / core** — P/PE routers and super-spines, which transit the fabric but attach to no leaf of it.

Under all of them is the **client** tier, which is not made of inventory nodes at all but of what the services are configured towards: a bridged sub-interface of a mac-vrf, or a routed port of an ip-vrf. Only ports facing nothing else we know of count, so the WAN sub-interface of a stitched ip-vrf stays a link to the DCGW's neighbour rather than becoming a customer, and IRBs, loopbacks and `system0` are left out. Several vlans on one cable are one client, and which ports belong to the same client is answered by whichever of these the fabric can tell us: the name an unmatched LLDP neighbour advertises, since a client that says who it is says the same to every leaf; failing that the **ESI** of the ethernet-segment the port is in, which is how a multi-homed client that runs no LLDP is still drawn as one box spanning its leaves rather than one box per leaf; and failing both, the port itself. Every client box just reads `client`: what it was named after is a guess drawn from an ESI or a port, which would read like an identity it does not have. Clicking one lists every sub-interface it attaches on, with its service, vlan and address, and walks to the leaf from there.

Between the leaves and the clients sit the **ethernet-segments**. A multi-homed client reaches its leaves over one bundle rather than a cable each, and that bundle is a configured object of its own — it has a name, an ESI every leaf on it agrees on, and it is where multi-homing goes wrong — so it gets a tier, with the client hanging off it. A segment box reads `ES` and the last two bytes of its ESI, which is what tells the segments of a fabric apart; the name each leaf gave it is in the panel, where two leaves disagreeing on it shows up.

Cables come from LLDP and are de-duplicated: both ends report the same link, and parallel cables between two nodes are drawn as one line marked `2×`. A link is coloured by the oper-state of the ports it lands on. Hovering a node pushes back everything it is not cabled to; clicking one opens its services and its per-port peer list, and clicking a peer from there walks the fabric.

A drawing is not always one fabric. Nodes that share no cable with each other are separate topologies, however many clients happen to be plugged into both of them: a server in two pods says nothing about a path between them, and drawing them as one claims a crossing that does not exist. So the split is made on the cables between nodes alone and each fabric gets a **tab** of its own, largest first, with **All** at the end for everything side by side. A client plugged into two fabrics is drawn on both tabs, and walking from it to a leaf on the other one switches tab with it. The tabs are named after whatever tells them apart — the site their nodes share, the name their nodes share (`frontend-leaf1` and `frontend-spine1` make a `frontend` tab), or their rank — and a naming that does not fit every fabric of the drawing is used for none of them, so no tab reads as a name beside another that reads as a placeholder. Nodes cabled to nothing at all, including one whose LLDP has not arrived yet, are gathered on an **Unattached** tab rather than getting one each, and while nothing has any cables the fabric stays whole.

A fabric wide enough that its node names stop being readable is navigated rather than read whole: the drawing zooms with the `−` / `+` buttons, with `ctrl` and the wheel (a trackpad pinch does the same), and with `-`, `+` and `0` on the keyboard, and it pans by dragging it. **fit** scales the whole fabric down to the window and follows it as the window and the detail panel change size; the zoom you pick instead is remembered across reloads. Dragging pans without selecting what the drag started on, so a node is only opened by a click that stays put.

Neighbours are matched back to the inventory through the name they advertise, so a containerlab node the inventory calls `clab-dc1-leaf1` is recognized when its neighbour reports it as `leaf1`. A neighbour that matches no node of the inventory is still drawn, as an *outside* node, and a node that has not streamed anything yet is drawn as *unclassified* rather than left out.

### Ask (LLM troubleshooting)

**Ask** (top bar) opens a read-only troubleshooting chat as soon as one LLM provider has a key on the server process. The agent uses the live report tables, then JSON-RPC `show` / `info` on a node if needed (containerlab enables JSON-RPC on mgmt; hardware fabrics may not). Keys never go to the browser.

The drawer shows what the agent is doing while it works — thinking, or which tool it is running, with how long each one took — and **Send** turns into **Stop** for as long as a turn is running. Answers are rendered as markdown. Drag the drawer's left edge to widen it (double-click the edge to reset).

| Provider | Key | Default model | API | Effort levels |
| --- | --- | --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `gpt-5.6-sol` | Responses | `none` … `max` |
| Claude | `ANTHROPIC_API_KEY` | `claude-sonnet-5` | Messages | `low` … `max` |
| Grok | `XAI_API_KEY` | `grok-4.6` | Chat Completions | `low` … `xhigh` |

Each provider also honours its own `_MODEL` and `_BASE_URL` variable (`OPENAI_MODEL`, `ANTHROPIC_BASE_URL`, `XAI_MODEL`, …). Claude also accepts `CLAUDE_API_KEY`; Grok also accepts `GROK_API_KEY`.

Set several keys and the drawer gets a provider selector; the browser remembers the last one you picked. `FCLI_LLM_PROVIDER=claude` sets which one is offered first, otherwise it is OpenAI, Claude, Grok in that order.

All three are reasoning models, and the drawer has an effort selector next to the provider. It defaults to `auto`, which leaves the choice to the model (medium on GPT-5.6, high on Claude and Grok); `OPENAI_REASONING_EFFORT`, `ANTHROPIC_EFFORT` and `XAI_REASONING_EFFORT` set the default per provider. Lower effort answers faster and costs less, higher effort holds up better on a fabric-wide "why is this broken" question.

OpenAI runs against the Responses API with `store=false`: fcli replays the model's reasoning itself between tool rounds, and nothing about your fabric is kept in OpenAI's response store. If you front OpenAI with a proxy that only speaks Chat Completions, set `OPENAI_API=chat`.

### How the live data works

1. When a report is opened for the first time, the server runs its getter once against a recording proxy of the gNMI connection. That yields the exact set of paths the report reads, so the subscription paths never have to be maintained separately from the reports.
2. Each path is bootstrapped with a regular gNMI `Get`, which seeds a per-node state tree and pins down the response shape the report getter expects.
3. A gNMI `Subscribe` (STREAM/SAMPLE) then keeps that tree current. Report getters run against the tree instead of the device, so a rendered table costs no device round-trip at all.
   One tree per node holds every subscription, so reading it back is not simply a matter of handing over the subtree: reports overlap (`/interface[name=lag*]` and `/interface[name=*]/statistics` both live under `interface`), and SR Linux streams whole subtrees for a subscription on one branch of them. What a report sees is therefore narrowed back down to what its own path selects — matching key predicates, the named branch, and nothing beside it — so a report reads what its own `Get` would have returned rather than what its neighbours put there.
4. A path that cannot be subscribed to falls back to a short-TTL `Get`, and every node is re-read every `--resync` seconds so a missed delete cannot leave a stale row behind. Nodes are re-read round-robin rather than all at once, so a sweep spreads its `Get`s over the interval.

Step 2 needs data to work with: SR Linux answers a `Get` for a subtree that holds nothing with an empty response, which does not reveal the shape the report getter expects. Control-plane driven tables regularly start out that way — no MACs learned yet, no ES destinations, no IPv6 neighbours, or a spine that has no bridge table at all. Such a path is *pending* rather than broken: it is left out of the subscription and served by the short-TTL `Get` of step 4, so the report renders as empty instead of failing. The first of those `Get`s that comes back with an entry pins down the shape, and the path joins the subscription from then on — the table starts streaming by itself, within the `Get` TTL of the first entry appearing, with no extra round-trip spent on polling for it. `GET /api/status` marks these paths `pending`, and `streaming` once they are live.

The per-report SAMPLE intervals are tuned per report (5s for interface counters, 60s for system info); `--sample-interval` overrides them all at once. `GET /api/status` shows what each node is currently subscribed to.

Opening a node's gNMI connection reaches it right away, to fetch its TLS certificate, so a node that is still booting when the server starts cannot be connected to yet. Those nodes are retried in the background — at most once every 30 seconds, and only while a report covering them is being rendered — so a server started alongside the fabric picks each node up as it comes up instead of reporting it unreachable until restarted. `GET /api/status` lists the nodes that are still unreachable under `unreachable`.

A node that goes away *after* it was connected — a reboot, or the whole lab being redeployed — is recovered the same way, at three levels. A `Get` that fails is not taken as proof that a path cannot be streamed, so the path stays a candidate and the next `--resync` sweep that gets an answer puts it back on the subscription; a report that could not be discovered is re-probed rather than written off for good. Until an answer comes back the table keeps its last known state rather than blanking, dated by the `generated` and `oldest_update` stamps the API returns, and the failing `Get` is remembered for the cache TTL so an unreachable node is not asked again by every report on every refresh.

Losing a node is noticed in three independent ways, because no one of them catches the others: the `Subscribe` RPC reporting an error, a `Get` failing or hanging, and updates that were due never arriving. The last one matters more than it sounds — if the *route* to a node disappears rather than the node refusing connections, the TCP connection simply falls silent, and with no keepalive on it gRPC goes on considering the call healthy. Since every path is subscribed in SAMPLE mode the target reports on a known interval whether anything changed or not, so updates going missing is the signal. This is what the node pane counts: `up` means the node is answering, not merely that a connection object exists for it.

Underneath that, the gRPC channel is given a `max_reconnect_backoff_ms` of 10s, because the default caps the reconnect backoff at two minutes — long enough that a node which is briefly gone reads as permanently gone while every call on it fails fast. As a last resort, a node whose `Get`s have been failing, *or hanging*, for longer than 30 seconds has its connection replaced outright: a gNMI call carries no deadline of its own, and a channel belonging to a container that no longer exists cannot be waited back into life.

### gNMI sessions

SR Linux accepts a limited number of concurrent gRPC sessions per gRPC server — `/system/grpc-server[name=mgmt]/session-limit`, 20 by default — and that budget is shared with every other gRPC client of the node. Every in-flight RPC counts, including a long-running `Subscribe`.

The server is built to stay at **one session per node**: all opened reports share a single `Subscribe` RPC, and at most one `Get` is in flight per node at a time. Since gNMI cannot add paths to a running subscription, growing the path set means replacing the RPC; those restarts are batched, so opening a page full of reports costs one re-subscribe rather than one per report. Paths that no report has read for `--idle-timeout` are dropped again, which keeps the streaming load on the node proportional to what is actually being watched.

`GET /api/status` reports `max_sessions_per_node`, and each node's own view is available on the device with:

```
❯ info from state /system grpc-server mgmt client *
```

### HTTP API

The UI is a client of a small JSON API, which is just as usable from scripts:

| Endpoint | Description |
| --- | --- |
| `GET /api/reports` | The available reports, version, and whether chat is enabled |
| `GET /api/inventory` | Inventory nodes, labels and connection state |
| `GET /api/status` | Per-node subscription state |
| `GET /api/overview` | Fabric KPI dashboard payload |
| `GET /api/topology` | The fabric graph: nodes with their inferred tier and the fabric they are cabled into, the clients hanging off them, and the links between them |
| `GET /api/report/{name}` | One rendered table as JSON |
| `GET /api/stream/{name}` | The same table, pushed as server-sent events |
| `POST /api/chat` | LLM troubleshooting turn (SSE: `start`, `token`, `tool`, `error`, `done`). Takes an optional `provider` (`openai`, `claude`, `grok`) and `effort`; 503 unless a provider key is set |

Report, stream, overview and topology endpoints accept `inv_filter=key=value,key=value`; the stream endpoint also accepts `refresh=<seconds>`.

```
❯ curl -s 'http://localhost:8080/api/report/bgp_peers?inv_filter=role%3Dleaf' | jq '.rows[0]'
```

## CLI reports

Same inventory, same getters, one shot. Output defaults to a Rich table; `-o json|yaml|csv` for structured output.

```
❯ fcli --help
Usage: fcli [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --cfg PATH         Nornir config file. Mutually exclusive with -t
  -i, --inv-filter TEXT  inventory filter, e.g. -i site=lab -i role=leaf
  -b, --box-type TEXT    box type of printed table ('python -m rich.box')
  -t, --topo-file PATH   CLAB topology file. Mutually exclusive with -c
  --cert-file PATH       CLAB certificate file
  -p, --gnmi-port        gNMI port [default: 57400]
  -o, --output           table | json | yaml | csv [default: table]
  -l, --log-level        DEBUG | INFO | WARNING | ERROR | CRITICAL
                         [default: ERROR]
  -f, --log-file PATH    also write the log to this file
  --version              Show the version and exit.
  --help                 Show this message and exit.

Commands:
  server        Serves live report tables over HTTP
  sys-info      Displays System Info of nodes
  bgp-peers     Displays BGP Peers and their status
  bgp-rib       Displays BGP RIB
  ipv4-rib      Displays IPv4 RIB entries
  ipv6-rib      Displays IPv6 RIB entries
  static-routes Displays static routes
  tunnel-table  Displays the IP tunnel-table (LDP, SR-ISIS, VXLAN, ...)
  ni            Displays Network Instances and interfaces
  subif         Displays Sub-Interfaces of nodes
  lag           Displays LAGs of nodes
  ifstats       Displays per-interface in/out bps from two consecutive samples
  mac           Displays MAC Table
  irb           Displays IRB sub-interfaces
  es            Displays Ethernet Segments
  es-dest       Displays ES Destinations on the bridge table
  vxlan         Displays VXLAN tunnel interfaces and unicast destinations
  lldp          Displays LLDP Neighbors
  arp           Displays ARP table
  nd            Displays IPv6 Neighbors
  routing-pol   Displays Routing Policies (json/yaml only)
```

Two kinds of filter, plus report-specific options:

- **inventory** (`-i`, global): `-i hostname=clab-4l2s-l1` or `-i role=leaf`, based on inventory data. Multiple filters are ANDed.
- **field** (`-f`, per report): `-f state="esta.*"`. Values are case-insensitive regexes; repeat `-f` to filter on several columns.
- **report-specific**: `bgp-rib` needs `-r evpn|ipv4|ipv6|l3vpn-v4|l3vpn-v6` (or the long `l3vpn-*-unicast` names) and optionally `-t 1|2|3|4|5` for EVPN route type. `ipv4-rib` / `ipv6-rib` take `-a` for an LPM lookup. `ifstats` takes `-s` for the sampling interval.

`fcli <report> --help` shows the options for that report.

### Examples

MAC entries on leafs in `macvrf-202` matching `1A:DC`:

```
fcli -i role=leaf mac -f NI=macvrf-202 -f Address="1A:DC:*"
```

BGP peers that are not established:

```
fcli bgp-peers -f state=active
```

Column headers use two lines in the live table (AFI label, then **R/A/T**). **U4** / **U6** = IPv4/IPv6 unicast, **EVPN**, **VPNv4** / **VPNv6** = L3VPN address families (values are received / active / sent, `disabled`, `down`, or `-`). JSON/YAML/CSV keys collapse the newline to a single space.

LPM lookup for `192.168.0.7` across every network-instance:

```
fcli ipv4-rib -a 192.168.0.7
```

Active IPv4 BGP routes for a prefix:

```
fcli bgp-rib -r ipv4 -f Pfx="192.168.255.4/32" -f 0_st="u*>"
```

EVPN RT=2 for a MAC:

```
fcli bgp-rib -r evpn -t 2 -f MAC="1A:DC:*"
```

VPN-IPv4 / VPN-IPv6 BGP RIB (per network-instance) use **RD** and **Pfx** columns. Nodes that do not expose the L3VPN RIB gNMI path (for example EVPN-only leaves) contribute **no rows** for that family instead of failing the whole report:

```
fcli bgp-rib -r l3vpn-v4 -f Pfx="10.*"
fcli bgp-rib -r l3vpn-v6
```

The `bgp-rib` table shows a curated set of priority fields so it stays readable. Non-table output (`-o json`, `-o yaml`, `-o csv`) and the MCP tool automatically include the full set of path attributes for each route: standard `communities`, Site-of-Origin (`soo`), BGP domain-path (`dpath`), `tunnel-encap` extended-community, route-target (`RT`), `as-path`, route status (`valid`/`best`/`used`), `tie-break` reason and `internal-tags`. Use `--detail`/`-d` to also include these columns in the table:

```
fcli -o json bgp-rib -r evpn -t 5
fcli -d bgp-rib -r evpn -t 5
```

Tunnel table with resolved egress interface, next-hop and pushed MPLS label-stack:

```
fcli tunnel-table -f type=ldp
```

### Debug logging

`-l DEBUG` traces what fcli does on the wire and why a report came out the way it
did: the inventory it resolved and the filter it applied, every gNMI `Get` with
its paths and round-trip time, the paths each report discovers, the `Subscribe`
RPCs the server opens and the notifications they carry, cache hits, reconnects,
and a traceback for every node that failed. Each line names the thread and call
site, so the per-node threads stay readable when they interleave.

```
fcli -l DEBUG -f /tmp/fcli.log bgp-peers
```

It is deliberately chatty - on the live server, expect a line per notification
per node - so `-f/--log-file` is usually easier to read than the terminal. The
log goes to stderr, which leaves `-o json|csv` on stdout pipeable while a trace
is running. Dependencies (gRPC, HTTP, the LLM clients) stay at INFO so their
frame-level logs do not bury fcli's own; set `FCLI_DEBUG_ALL=1` to let those
through as well.

## MCP server

`fcli-mcp` exposes the CLI reports as [Model Context Protocol](https://modelcontextprotocol.io/) tools (`stdio` by default, or HTTP). An agent can query fabric state without wrapping `fcli` itself.

```bash
fcli-mcp --topo-file topo.clab.yml
fcli-mcp --config-file nornir_config.yaml
fcli-mcp --transport http --port 8080
```

It can start with no topology loaded. These tools then pick or switch the fabric at runtime:

- `list_topologies` — discover `*.clab.yml` and `nornir_config.yaml` files in a directory
- `load_topology` — initialize or switch from a containerlab file
- `load_config` — initialize or switch from a Nornir config file
- `show_topology` — nodes, labels, and the keys `inv_filter` can use

Report tools take the same `inv_filter` and `field_filter` as the CLI (comma-separated `key=value`).

### Claude Desktop

```json
{
  "mcpServers": {
    "fcli": {
      "command": "fcli-mcp"
    }
  }
}
```

### Gemini CLI

```json
{
  "mcpServers": {
    "fcli": {
      "command": "fcli-mcp"
    }
  }
}
```

## Reports

One registry drives all three surfaces, so a report cannot drift between CLI, MCP and the server. Not every report is on every surface: `overview` and `topology` only exist in the browser; `bgp-rib` takes an address family the streaming server cannot be given, so the server gets one pre-bound report per family instead; `routing-pol` is nested JSON that no table can represent.

| Report | CLI | Server | What it shows |
| --- | --- | --- | --- |
| Overview | | yes | Fabric KPIs (nodes, interfaces, BGP sessions) |
| Topology | | yes | LLDP graph with inferred leaf / spine / DCGW / client tiers |
| System Info | `sys-info` | yes | Chassis, serial, software version, last boot |
| Interface Stats | `ifstats` | yes | Per-interface rates and error/discard counters |
| Sub-Interfaces | `subif` | yes | Type, addresses, oper-state |
| LAGs | `lag` | yes | LAG members and LACP |
| Network Instances | `ni` | yes | NIs, their EVPN EVI and the interfaces bound to them |
| BGP Peers | `bgp-peers` | yes | Session state and per-AF R/A/T |
| BGP RIB | `bgp-rib -r …` | split per family / EVPN type | RIB-in-post with path attributes |
| IPv4 / IPv6 RIB | `ipv4-rib`, `ipv6-rib` | yes | Route table with resolved next-hops; `-a` for LPM |
| Static Routes | `static-routes` | yes | Configured statics and their state |
| Tunnel Table | `tunnel-table` | yes | VXLAN, LDP, SR-ISIS, RSVP, … |
| Routing Policies | `routing-pol` | | Nested policy JSON (`-o json\|yaml` only) |
| Services | | yes | MAC-VRF and IP-VRF grouped by route-target |
| Bridge Domains | | yes | MAC-VRFs with access ports, ethernet-segments and VXLAN overlays |
| Routers | | yes | IP-VRFs with bound MAC-VRFs, virtual ethernet-segments and overlays |
| MAC Table | `mac` | yes | Bridge-table MAC entries |
| IRB Interfaces | `irb` | yes | IRB sub-interfaces and anycast gateways |
| Ethernet Segments | `es` | yes | ESI, MH mode, DF state, EVI of a virtual ES |
| L2-ES Destinations | `es-dest` | yes | ES destinations in the bridge table |
| VXLAN Tunnels | `vxlan` | yes | VXLAN interfaces and unicast destinations |
| LLDP Neighbors | `lldp` | yes | Neighbours per interface |
| ARP Table | `arp` | yes | IPv4 neighbours per sub-interface |
| IPv6 Neighbors | `nd` | yes | ND entries per sub-interface |

## Tested SR Linux releases

The reports hard-code gNMI paths and the YANG structure they expect back, and both move between SR Linux releases. When they move, the failure is usually silent: a path that no longer carries a value leaves a column empty rather than raising anything.

So CI does not mock the device. Every report is run once against a real, fully configured EVPN-VXLAN fabric per release, and the entire gNMI exchange — every `Get` and the payload or error the device answered with — is recorded. Each pull request replays those recordings through the production report code with no lab present:

| SR Linux release | Nodes recorded | Reports replayed per node |
|---|---|---|
| 25.3.2 | leaf + spine | 32 |
| 25.10.3 | leaf + spine | 32 |
| 26.3.1 | leaf + spine | 32 |
| 26.7.1 | leaf + spine | 32 |

That is 522 test cases, the bulk of the suite. Each one asserts that the report does not raise, that it still produces the exact table the live device produced, and that the set of paths a release rejects is the documented one — so a path that newly breaks, or one that quietly started working, fails the build instead of emptying a column.

All four releases produce **identical columns for every report**, despite 43–89 leaves being added and 3–28 removed under those paths between consecutive releases. The two `bgp-rib -r l3vpn-v4|l3vpn-v6` variants are rejected by all four, because the `bgp-rib` model only carries the l3vpn containers on a node configured for MPLS IP-VPN; those reports degrade to an empty table and the rejection is pinned as expected.

The lab, the per-release datamodel changes and how to re-record are described in [`tests/fixtures/releases/MATRIX.md`](tests/fixtures/releases/MATRIX.md).
