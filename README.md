
![Demo](https://github.com/srl-labs/nornir-srl/blob/main/imgs/fcli_demo.gif)
# nornir-srl

[![ci](https://github.com/srl-labs/nornir-srl/actions/workflows/ci.yml/badge.svg)](https://github.com/srl-labs/nornir-srl/actions/workflows/ci.yml)
[![SR Linux](https://img.shields.io/badge/SR%20Linux-25.3.2%20%7C%2025.10.3%20%7C%2026.3.1%20%7C%2026.7.1-blue)](#tested-sr-linux-releases)
[![PyPI](https://img.shields.io/pypi/v/nornir-srl)](https://pypi.org/project/nornir-srl/)

This module provides a [Nornir](https://nornir.readthedocs.io/en/latest/) connection [plugin](https://nornir.tech/nornir/plugins/) for Nokia SRLinux devices. It uses the gNMI management interface of SRLinux to fetch state and push configurations and the [PyGNMI](https://github.com/akarneliuk/pygnmi) Python module to interact with gNMI. 

Rather than limiting the connection plugin to primitives like `open_connection`, `close_connection`, `get`, `set`, etc, this module provides also methods to get information from the device for common resources. Since the device model tends to change between releases, it was considered a better approach to provide this functionality as part of the connection plugin and hide complexity of model changes to the user or Nornir tasks. 

In addition to the connection plugin, there is a set of Nornir tasks that use the connection plugin to perform common operations on the device, like get BGP peers, get MAC table, get subinterfaces, etc. These Nornir tasks are called by a command-line interface `fcli` that provides a network-wide CLI to perform show commands across an entire set or subset for SRLinux nodes.

> **Note:** The current functionality is focused on a read-only _network-wide CLI_ to perform show commands across an entire set or subset for SRLinux nodes, as defined in the Nornir inventory and through command-line filter options. It shows output in a tabular format for easy reading.
Following versions may focus on configuration management and command execution on the nodes.

# Tested SR Linux releases

The reports hard-code gNMI paths and the YANG structure they expect back, and both
move between SR Linux releases. When they move, the failure is usually silent: a path
that no longer carries a value leaves a column empty rather than raising anything.

So CI does not mock the device. Every report is run once against a real, fully
configured EVPN-VXLAN fabric per release, and the entire gNMI exchange - every `Get`
and the payload or error the device answered with - is recorded. Each pull request
replays those recordings through the production report code with no lab present:

| SR Linux release | Nodes recorded | Reports replayed per node |
|---|---|---|
| 25.3.2 | leaf + spine | 32 |
| 25.10.3 | leaf + spine | 32 |
| 26.3.1 | leaf + spine | 32 |
| 26.7.1 | leaf + spine | 32 |

That is 522 test cases, the bulk of the suite. Each one asserts that the report does
not raise, that it still produces the exact table the live device produced, and that
the set of paths a release rejects is the documented one - so a path that newly breaks,
or one that quietly started working, fails the build instead of emptying a column.

All four releases produce **identical columns for every report**, despite 43-89 leaves
being added and 3-28 removed under those paths between consecutive releases. The two
`bgp-rib -a l3vpn-ipv4|l3vpn-ipv6` variants are rejected by all four, because the
`bgp-rib` model only carries the l3vpn containers on a node configured for MPLS IP-VPN;
those reports degrade to an empty table and the rejection is pinned as expected.

The lab, the per-release datamodel changes and how to re-record are described in
[`tests/fixtures/releases/MATRIX.md`](tests/fixtures/releases/MATRIX.md).

# Quickstart

## Prerequisites

- have [Containerlab](https://containerlab.dev/) installed
- have a running containerlab topology with SRLinux nodes
- Internet access to pull the `nornir-srl` container image

## Create a shell alias for `fcli`

- go to the directory where your containerlab topology file is located
- create an alias for `fcli` as follows and modify the `CLAB_TOPO` to match your topology file name
- modify the `--network` option to match your containerlab network name (default is the name of the lab)
- latest version of `nornir-srl` container image is [here](https://github.com/srl-labs/nornir-srl/pkgs/container/nornir-srl). Modify the tag accordingly if you want to use a different version

```
CLAB_TOPO=topo.yaml && alias fcli="docker run -it --network $(grep '^name:' $CLAB_TOPO | awk '{print $2}') --rm -v /etc/hosts:/etc/hosts:ro -v ${PWD}/${CLAB_TOPO}:/topo.yml ghcr.io/srl-labs/nornir-srl:latest -t /topo.yml"
```

Running `fcli` without additional arguments will start an interactive shell inside the container.

## Run `fcli`

```
❯ fcli --help
Usage: fcli [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --cfg PATH         Nornir config file. Mutually exclusive with -t
                         [default: nornir_config.yaml]
  -i, --inv-filter TEXT  inventory filter, e.g. -i site=lab -i role=leaf.
                         Possible filter-fields are defined in inventory.
                         Multiple filters are ANDed
  -b, --box-type TEXT    box type of printed table, e.g. -b
                         minimal_double_head. 'python -m rich.box' for options
  -t, --topo-file PATH   CLAB topology file, e.g. -t topo.yaml. Mutually
                         exclusive with -c
  --cert-file PATH       CLAB certificate file, e.g. -c ca-root.pem
  --version              Show the version and exit.
  --help                 Show this message and exit.

Commands:
  bgp-peers     Displays BGP Peers and their status
  bgp-rib       Displays BGP RIB
  ipv4-rib      Displays IPv4 RIB entries, LPM lookup
  lldp          Displays LLDP Neighbors
  mac           Displays MAC Table
  ni            Displays Network Instances and interfaces
  subif         Displays Sub-Interfaces of nodes
  sys-info      Displays System Info of nodes
  tunnel-table  Displays the IP tunnel-table (LDP, SR-ISIS, VXLAN, ...)
```

# Installation

## Docker-based installation

This is the easiest way to get started. It requires [Docker](https://docs.docker.com/get-docker/) and optionally  [Containerlab](https://containerlab.dev/) to be installed on your system.

> NOTE: if you have issues connecting to the docker network of containerlab from the `nornir-srl` container that uses the standard bridge `docker0`, make sure proper `iptables` rules are in place to permit traffic between different Docker networks, which is **by default blocked**. For example, on Ubuntu 20.04, you can use the following command:

```
iptables -I DOCKER-USER -o docker0 -j ACCEPT -m comment --comment "allow inter-network comms"
```

Alternatively, you can attach the `nornir-srl` container to the containerlab network to avoid adding iptables rules (cf. aliases below).

To run `fcli`, create an alias in your shell session. For example, assuming you're using containerlab and  you have a `clab_topo.yml` file in your current directory and lab is up and running:

```
CLAB_TOPO=clab_topo.yml && alias fcli="docker run -it --network $(grep '^name:' $CLAB_TOPO | awk '{print $2}') --rm -v /etc/hosts:/etc/hosts:ro -v ${PWD}/${CLAB_TOPO}:/topo.yml ghcr.io/srl-labs/nornir-srl:0.2.1 -t /topo.yml"
```

Running `fcli` without additional arguments will start an interactive shell inside the container.

This command assumes that the containerlab topology file is named `clab_topo.yml` and is in the current directory. If not, change the `CLAB_TOPO` variable accordingly. Also, it assumes that the containerlab topology is using the default containerlab docker-network naming, i.e. name of the lab. If you have overridden the management network with `.mgmt.network` in the topology file, change the `--network` option accordingly.

## Python-based installation with `uv` (recommended)

[`uv`](https://github.com/astral-sh/uv) is a standalone Python package manager.
Install `uv` and then install `nornir-srl` directly from GitHub:

```bash
# On Linux and macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/srl-labs/nornir-srl
```

After this you can simply run `fcli` or `fcli-mcp`:

```bash
fcli --help
fcli-mcp --help
```

> **Note:** Ensure your shell's `PATH` includes the directory where `uv` or `pip` installs binaries (typically `~/.local/bin` on Linux/macOS).

## Python-based installation with `pip`

Create a Python virtual-env using your favorite workflow, For example:
```
mkdir nornir-srl && cd nornir-srl
python3 -m venv .venv
source .venv/bin/activate
```
Following command will install the the `nornir-srl` module and all its dependencies, including Nornir core.

```
pip install wheel
pip install -U nornir-srl
```

## Nornir-based inventory mode

In this mode, a Nornir configuration file must be provided with the `-c` option. The Nornir inventory is polulated by the `InventoryPlugin` and associated options as specified in the config file. See below for an example with the included `YAMLInventory` plugin and the associated inventory files. This mode is typically used for real hardware-based fabric.

Create the Nornir confguration file, for example:

```yaml
# nornir_config.yaml
inventory:
    #    plugin: SimpleInventory
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

Create the inventory files as referenced in the above configuration file, for example:

```yaml
## hosts.yaml
clab-4l2s-l1:
    hostname: clab-4l2s-l1
    groups: [srl, fabric, leafs]
clab-4l2s-l2:
    hostname: clab-4l2s-l2
    groups: [srl, fabric, leafs]
clab-4l2s-s1:
    hostname: clab-4l2s-s1
    groups: [srl, fabric, spines]
```
```yaml
## groups.yaml
global:
    data:
        domain: clab
srl:
    connection_options:
        srlinux:
            port: 57400
            username: admin
            password: admin
            extras:
                path_cert: "./root-ca.pem"
spines:
    groups: [ global ]
    data:
        role: spine
        type: ixr-d3
leafs:
    groups: [ global ]
    data:
        role: leaf
        type: ixr-d2
```
The root certificate is specified once for all devices in group `srl` via the `connection_options.srlinux.extras.path_cert` parameter.

## CLAB-based inventory mode

In this mode, the Nornir inventory is populated by a containerlab topology file and no further configuration files are needed. The containerlab topo file is specified with the `-t` option. 

`fcli` converts the topology file to a _hosts_ and _groups_ file and only nodes of kind=srl are populated in the host inventory. Furthermore, the `prefix` parameter in the topo file is considered to generate the hostnames. The presence of _labels_ in the topo file is mapped into node-specific attribs that can be used in inventory filters (`-i` option).

# Usage

` fcli` supports a set of reports that can be run against a set of SRLinux nodes. The set of nodes is defined by the Nornir inventory and optionally filtered by the `-i` option.

```
❯ fcli
Usage: fcli [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --cfg PATH         Nornir config file. Mutually exclusive with -t
                         [default: nornir_config.yaml]
  -i, --inv-filter TEXT  inventory filter, e.g. -i site=lab -i role=leaf.
                         Possible filter-fields are defined in inventory.
                         Multiple filters are ANDed
  -b, --box-type TEXT    box type of printed table, e.g. -b
                         minimal_double_head. 'python -m rich.box' for options
  -t, --topo-file PATH   CLAB topology file, e.g. -t topo.yaml. Mutually
                         exclusive with -c
  --cert-file PATH       CLAB certificate file, e.g. -c ca-root.pem
  --version              Show the version and exit.
  --help                 Show this message and exit.

Commands:
  bgp-peers     Displays BGP Peers and their status
  bgp-rib       Displays BGP RIB
  ipv4-rib      Displays IPv4 RIB entries, LPM lookup
  lldp          Displays LLDP Neighbors
  mac           Displays MAC Table
  ni            Displays Network Instances and interfaces
  subif         Displays Sub-Interfaces of nodes
  sys-info      Displays System Info of nodes
  tunnel-table  Displays the IP tunnel-table (LDP, SR-ISIS, VXLAN, ...)
```

To run a specific report, use the corresponding command, e.g. `fcli mac` to display the MAC table of all nodes in the inventory. The output is a table with columns relevant to the report.

```
❯ fcli -b ascii mac
                                                              MAC Table                                                              
+-----------------------------------------------------------------------------------------------------------------------------------+
| Node            | NI          | Address           | Dest                                                  | Type                  |
|-----------------+-------------+-------------------+-------------------------------------------------------+-----------------------|
| clab-4l2s-l1    | macvrf-202  | 00:00:5E:00:01:01 | irb                                                   | irb-interface-anycast |
|                 |             | 1A:B4:09:FF:00:42 | vxlan-interface:vxlan1.202 vtep:192.168.255.2 vni:202 | evpn-static           |
|                 |             | 1A:B9:08:FF:00:42 | irb                                                   | irb-interface         |
|                 |             | 1A:DC:0E:FF:00:41 | lag1.1                                                | evpn                  |
|                 | macvrf-203  | 1A:B9:08:FF:00:42 | irb                                                   | irb-interface         |
|-----------------+-------------+-------------------+-------------------------------------------------------+-----------------------|
| clab-4l2s-l2    | macvrf-202  | 00:00:5E:00:01:01 | irb                                                   | irb-interface-anycast |
|                 |             | 1A:B4:09:FF:00:42 | irb                                                   | irb-interface         |
|                 |             | 1A:B9:08:FF:00:42 | vxlan-interface:vxlan1.202 vtep:192.168.255.1 vni:202 | evpn-static           |
|                 |             | 1A:DC:0E:FF:00:41 | lag1.1                                                | learnt                |
|-----------------+-------------+-------------------+-------------------------------------------------------+-----------------------|
| clab-4l2s-l4    | macvrf-201  | 1A:3B:0B:FF:00:41 | irb                                                   | irb-interface         |
|-----------------+-------------+-------------------+-------------------------------------------------------+-----------------------|
| clab-4l2s-tor12 | macvrf-9998 | 00:00:5E:00:01:01 | lag1.1                                                | learnt                |
|                 |             | 1A:DC:0E:FF:00:41 | irb                                                   | irb-interface         |
|                 | macvrf-9999 | 1A:DC:0E:FF:00:41 | irb                                                   | irb-interface         |
+-----------------------------------------------------------------------------------------------------------------------------------+
```

Some reports have additional options. You can get help on the options with the `--help` option __after__ the report name, e.g. `fcli bgp-rib --help`:

```
❯ fcli bgp-rib --help
Usage: fcli bgp-rib [OPTIONS]

  Displays BGP RIB

Options:
  -f, --field-filter TEXT       filter fields with <field-name>=<regex-
                                pattern>, e.g. -f state=up -f
                                admin_state="ena.*". Fieldnames correspond to
                                column names of a report
  -r, --route-fam TEXT          evpn | ipv4 | ipv6 | l3vpn-v4 | l3vpn-v6 (IP-VPN
                                unicast; full names l3vpn-ipv4-unicast /
                                l3vpn-ipv6-unicast also accepted)  [required]
  -t, --route-type [1|2|3|4|5]  Route type for EVPN routes
  --help                        Show this message and exit.
```

## Filtering

Optionally, you can specify filters to control the output. There are 2 types of filters:

- inventory filters, specified with the global `-i` option, filter on the inventory, e.g. `-i hostname=clab-4l2s-l1`  or `-i role=leaf` based on inventory data
- field filters, specified with the report-specific `-f` option. This filters based on the fields shown in the report and a regex pattern, e.g. `-f state="esta.*"`. Multiple field filters can be specified by repeated `-f` options
- report-specific options are options specific to a report, if applicable. Currently, the only report that needs extra arguments is 'bgp-rib', i.e. `route_fam=evpn|ipv4|ipv6|l3vpn-v4|l3vpn-v6` (or the long `l3vpn-*-unicast` names) and `route_type=1|2|3|4|5` for EVPN only. The latter relates to EVPN route-types and is optional. Defaults to '2' (mac-ip-routes). 

## Examples

### mac-table

Find all MAC entries on all leafs in mac-vrf `macvrf-202` that matches the pattern `1A:DC`

`fcli -i role=leaf mac -f NI=macvrf-202 -f Address="1A:DC:*"`

```
                             MAC Table                             
     Fields filter:{'NI': 'macvrf-202', 'Address': '1A:DC:*'}      
                 Inventory filter:{'role': 'leaf'}                 
+-----------------------------------------------------------------+
| Node         | NI         | Address           | Dest   | Type   |
|--------------+------------+-------------------+--------+--------|
| clab-4l2s-l1 | macvrf-202 | 1A:DC:0E:FF:00:41 | lag1.1 | evpn   |
|--------------+------------+-------------------+--------+--------|
| clab-4l2s-l2 | macvrf-202 | 1A:DC:0E:FF:00:41 | lag1.1 | learnt |
+-----------------------------------------------------------------+
```

### bgp-peers

Show all BGP peers on all nodes that are in state `active`:

`fcli bgp-peers -f state=active`

```
                                                                  BGP Peers                                                                   
                                                      Fields filter:{'state': 'active'}                                                       
+---------------------------------------------------------------------------------------------------------------------------------------------------------+
|              |           |                 | U4    | U6    | EVPN  | VPNv4 | VPNv6 |               |         |               |          |         |        |
|              |           |                 | R/A/T | R/A/T | R/A/T | R/A/T | R/A/T |               |         |               |          |         |        |
| Node         | NI        | 1_peer          |       |       |       |       |       | export_policy | group   | import_policy | local_as | peer_as | state  |
|--------------+-----------+-----------------+-------+-------+-------+-------+-------+---------------+---------+---------------+----------+---------+--------|
| clab-4l2s-l4 | ipvrf-200 | 10.200.4.100    | disabled | -        | disabled | -        | -        | v200-out      | clients |               | 6848     | 65534   | active |
|--------------+-----------+-----------------+----------+----------+----------+----------+----------+---------------+---------+---------------+----------+---------+--------|
| clab-4l2s-s1 | default   | 192.168.0.225   | disabled | -        | disabled | -        | -        | pass-all      | dcgw    | pass-all      | 65100    | 65200   | active |
|              |           | 192.168.255.201 | disabled | -        | 0/0/0    | -        | -        | pass-evpn     | overlay | pass-evpn     | 100      | 100     | active |
+---------------------------------------------------------------------------------------------------------------------------------------------------------+
```

Column headers use two lines in the live table (AFI label, then **R/A/T**). **U4** / **U6** = IPv4/IPv6 unicast, **EVPN**, **VPNv4** / **VPNv6** = L3VPN address families (values are received / active / sent, `disabled`, `down`, or `-`). JSON/YAML/CSV keys collapse the newline to a single space.

### ipv4-rib

Show all IPv4 routes on all nodes across all network-instances that matches address `192.168.0.7` with LPM (longest-prefix-match):

`fcli ipv4-rib -a 192.168.0.7`

```
                                       IPv4 RIB - hunting for 192.168.0.7                                       
+--------------------------------------------------------------------------------------------------------------+
| Node         | NI      | Act | Prefix         | itf                | metric | next-hop        | pref | type  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-l1 | default | Yes | 192.168.0.6/31 |                    | 0      | ['192.168.0.0'] | 170  | bgp   |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-l2 | default | Yes | 192.168.0.6/31 |                    | 0      | ['192.168.0.2'] | 170  | bgp   |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-l3 | default | Yes | 192.168.0.6/31 |                    | 0      | ['192.168.0.4'] | 170  | bgp   |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-l4 | default | Yes | 192.168.0.7/32 |                    | 0      | [None]          | 0    | host  |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-s1 | default | Yes | 192.168.0.6/31 | ['ethernet-1/4.0'] | 0      | ['192.168.0.6'] | 0    | local |
|              | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
|--------------+---------+-----+----------------+--------------------+--------+-----------------+------+-------|
| clab-4l2s-s2 | mgmt    | Yes | 0.0.0.0/0      | ['mgmt0.0']        | 0      | ['172.20.21.1'] | 5    | dhcp  |
+--------------------------------------------------------------------------------------------------------------+
```

### bgp-rib

Show all active BGP routes with AF=ipv4 that are active and used for prefix `192.168.255.4/32`:

`fcli bgp-rib -r ipv4 -f Pfx="192.168.255.4/32" -f 0_st="u*>"`

```
                                                        BGP RIB - IPV4                                                         
                                   Fields filter:{'Pfx': '192.168.255.4/32', '0_st': 'u*>'}                                    
+-----------------------------------------------------------------------------------------------------------------------------+
| Node         | NI      | 0_st | Pfx              | as-path          | communities | lpref | med | neighbor    | next-hop    |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-l1 | default | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.0.0 | 192.168.0.0 |
|              |         | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.1.0 | 192.168.1.0 |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-l2 | default | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.0.2 | 192.168.0.2 |
|              |         | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.1.2 | 192.168.1.2 |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-l3 | default | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.0.4 | 192.168.0.4 |
|              |         | u*>  | 192.168.255.4/32 | [65100, 65004] i |             | 100   | 0   | 192.168.1.4 | 192.168.1.4 |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-l4 | default | u*>  | 192.168.255.4/32 | i                |             | 100   | 0   | 0.0.0.0     | 0.0.0.0     |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-s1 | default | u*>  | 192.168.255.4/32 | [65004] i        |             | 100   | 0   | 192.168.0.7 | 192.168.0.7 |
|--------------+---------+------+------------------+------------------+-------------+-------+-----+-------------+-------------|
| clab-4l2s-s2 | default | u*>  | 192.168.255.4/32 | [65004] i        |             | 100   | 0   | 192.168.1.7 | 192.168.1.7 |
+-----------------------------------------------------------------------------------------------------------------------------+
```

Show all EVPN RT=2 routes for MAC address that starts with "1A:DC":

`fcli bgp-rib -r evpn -t 2 -f MAC="1A:DC:*"`

```
                                                                           BGP RIB - EVPN route-type 2                                                                            
                                                                         Fields filter:{'MAC': '1A:DC*'}                                                                          
+--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------+
| Node         | NI      | 0_st | ESI                           | IP          | MAC               | RD                | as-path | next-hop      | origin | peer            | vni |
|--------------+---------+------+-------------------------------+-------------+-------------------+-------------------+---------+---------------+--------+-----------------+-----|
| clab-4l2s-l1 | default | u*>  | 01:24:24:24:24:24:24:00:00:01 | 0.0.0.0     | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.101 | 202 |
|              |         | *    | 01:24:24:24:24:24:24:00:00:01 | 0.0.0.0     | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.102 | 202 |
|              |         | u*>  | 01:24:24:24:24:24:24:00:00:01 | 10.200.1.10 | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.101 | 202 |
|              |         | *    | 01:24:24:24:24:24:24:00:00:01 | 10.200.1.10 | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.102 | 202 |
|--------------+---------+------+-------------------------------+-------------+-------------------+-------------------+---------+---------------+--------+-----------------+-----|
| clab-4l2s-s1 | default | *>   | 01:24:24:24:24:24:24:00:00:01 | 0.0.0.0     | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.2   | 202 |
|              |         | *>   | 01:24:24:24:24:24:24:00:00:01 | 10.200.1.10 | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.2   | 202 |
|--------------+---------+------+-------------------------------+-------------+-------------------+-------------------+---------+---------------+--------+-----------------+-----|
| clab-4l2s-s2 | default | *>   | 01:24:24:24:24:24:24:00:00:01 | 0.0.0.0     | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.2   | 202 |
| clab-4l2s-s2 | default | *>   | 01:24:24:24:24:24:24:00:00:01 | 10.200.1.10 | 1A:DC:0E:FF:00:41 | 192.168.255.2:202 | i       | 192.168.255.2 | igp    | 192.168.255.2   | 202 |
+--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------+
```

VPN-IPv4 / VPN-IPv6 BGP RIB (per network-instance) use **RD** and **Pfx** columns instead of a single `Prefix` field:

`fcli bgp-rib -r l3vpn-v4 -f Pfx="10.*"`  
`fcli bgp-rib -r l3vpn-v6`  (same as `-r l3vpn-ipv6-unicast`)

Nodes that do not expose the L3VPN RIB gNMI path (for example EVPN-only leaves) contribute **no rows** for that family instead of failing the whole report.

#### bgp-rib path attributes

The `bgp-rib` table shows a curated set of priority fields so it stays readable.
Non-table output (`-o json`, `-o yaml`, `-o csv`) and the MCP tool automatically
include the full set of path attributes for each route: standard `communities`,
Site-of-Origin (`soo`), BGP domain-path (`dpath`), `tunnel-encap` extended-community,
route-target (`RT`), `as-path`, route status (`valid`/`best`/`used`), `tie-break`
reason and `internal-tags`. Use `--detail`/`-d` to also include these columns in the
table output.

```
fcli -o json bgp-rib -r evpn -t 5            # full attributes (json)
fcli -d bgp-rib -r evpn -t 5                 # full attributes in the table too
```

### tunnel-table

Show the IP tunnel-table with the resolved egress interface, next-hop and pushed
MPLS label-stack. Useful to verify which transport (LDP, SR-ISIS, RSVP, VXLAN, ...)
a remote endpoint is reached over, and on which port.

`fcli tunnel-table -f type=ldp`

```
                                     Tunnel Table
+-----------------------------------------------------------------------------------------+
| Node  | NI      | Prefix          | type | owner   | pref | metric | next-hop     | egress-itf        | label   |
|-------+---------+-----------------+------+---------+------+--------+--------------+-------------------+---------|
| dcgw1 | default | 192.0.2.152/32  | ldp  | ldp_mgr | 9    | 10     | ['10.255.0.1'] | ['ethernet-1/5.0'] | ['20000'] |
+-----------------------------------------------------------------------------------------+
```

# Web server with live tables

`fcli server` serves the same reports as a web UI, kept up to date by gNMI
**subscriptions** instead of one-shot polls. Every node in the inventory gets a
single `Subscribe` RPC carrying the paths the opened reports need, and the
browser is pushed a new table over server-sent events whenever the data
actually changes.

The tables are the CLI reports, rendered by the same getters, so a column in the
browser means what it means in `fcli`. The one CLI report that is not served is
`routing-pol`, which is nested JSON rather than a table. `bgp-rib` is split into
one report per route family (and per EVPN route type) so that no report needs
arguments.

```
❯ fcli -t topo.clab.yml server
fcli server on http://127.0.0.1:8080 (6 node(s))
```

The global options are the same as for the CLI reports, so the server can be
pointed at a containerlab topology (`-t`), a Nornir config (`-c`) and be scoped
to a subset of the fabric (`-i`):

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
│ --help                              Show this message and exit.              │
╰──────────────────────────────────────────────────────────────────────────────╯
```


> The server binds to localhost by default. It has no authentication of its
> own, so put it behind a reverse proxy (or keep it on localhost) before
> exposing it with `--listen 0.0.0.0`.

## In the browser

* **Reports** are listed in the sidebar, grouped by category, with a filter box
  on top. The selected report is kept in the URL fragment
  (`http://localhost:8080/#bgp_peers`), so a view can be bookmarked or shared.
* **Sorting**: click a column header to sort, click again to reverse. Sorting is
  natural, so `ethernet-1/10` comes after `ethernet-1/2`.
* **Filtering**: each column has its own filter box, and the search box above
  the table filters on all visible columns at once. Both accept a regular
  expression and fall back to a substring match if the expression is not valid
  (yet).
* **Inventory filter**: the same `key=value` filter as the CLI's `-i`, applied
  live — e.g. `role=leaf`.
* **Live updates**: cells that changed since the previous update flash, and new
  rows flash as a whole. **Pause** freezes the table without dropping the
  subscriptions.
* **Columns** hides columns you do not need, **CSV** downloads exactly what the
  table currently shows (filters, column selection and all).
* **Ask** (top bar) opens a read-only troubleshooting chat as soon as one LLM
  provider has a key on the server process. The agent uses the live report
  tables, then JSON-RPC `show`/`info` on a node if needed. Keys never go to the
  browser.

  The drawer shows what the agent is doing while it works — thinking, or which
  tool it is running, with how long each one took — and **Send** turns into
  **Stop** for as long as a turn is running. Answers are rendered as markdown.
  Drag the drawer's left edge to widen it (double-click the edge to reset).

  | Provider | Key | Default model | API | Effort levels |
  | --- | --- | --- | --- | --- |
  | OpenAI | `OPENAI_API_KEY` | `gpt-5.6-sol` | Responses | `none` … `max` |
  | Claude | `ANTHROPIC_API_KEY` | `claude-sonnet-5` | Messages | `low` … `max` |
  | Grok | `XAI_API_KEY` | `grok-4.6` | Chat Completions | `low` … `xhigh` |

  Each provider also honours its own `_MODEL` and `_BASE_URL` variable
  (`OPENAI_MODEL`, `ANTHROPIC_BASE_URL`, `XAI_MODEL`, …).

  Set several keys and the drawer gets a provider selector; the browser
  remembers the last one you picked. `FCLI_LLM_PROVIDER=claude` sets which one
  is offered first, otherwise it is OpenAI, Claude, Grok in that order.

  All three are reasoning models, and the drawer has an effort selector next to
  the provider. It defaults to `auto`, which leaves the choice to the model
  (medium on GPT-5.6, high on Claude and Grok); `OPENAI_REASONING_EFFORT`,
  `ANTHROPIC_EFFORT` and `XAI_REASONING_EFFORT` set the default per provider.
  Lower effort answers faster and costs less, higher effort holds up better on
  a fabric-wide "why is this broken" question.

  OpenAI runs against the Responses API with `store=false`: fcli replays the
  model's reasoning itself between tool rounds, and nothing about your fabric is
  kept in OpenAI's response store. If you front OpenAI with a proxy that only
  speaks Chat Completions, set `OPENAI_API=chat`.

## Topology

The **Topology** page draws the fabric from LLDP, one tier per row, and works out
what each node is from what it runs rather than from an inventory label:

* a node with **mac-vrfs**, and optionally ip-vrfs, is a **leaf** — the tier where
  a service meets a port;
* a node whose services carry **two enabled `bgp-vpn` instances** is a **DCGW**:
  the second instance is the WAN side of a stitched service, which only a gateway
  out of the DC has;
* a node with **no mac-vrf and no ip-vrf** that sees two or more leaves is a
  **spine**: it interconnects them without terminating anything;
* anything else without services is **WAN / core** — P/PE routers and
  super-spines, which transit the fabric but attach to no leaf of it.

Under all of them is the **client** tier, which is not made of inventory nodes at
all but of what the services are configured towards: a bridged sub-interface of a
mac-vrf, or a routed port of an ip-vrf. Only ports facing nothing else we know of
count, so the WAN sub-interface of a stitched ip-vrf stays a link to the DCGW's
neighbour rather than becoming a customer, and IRBs, loopbacks and `system0` are
left out. Several vlans on one cable are one client, and which ports belong to
the same client is answered by whichever of these the fabric can tell us: the
name an unmatched LLDP neighbour advertises, since a client that says who it is
says the same to every leaf; failing that the **ESI** of the ethernet-segment the
port is in, which is how a multi-homed client that runs no LLDP is still drawn as
one box spanning its leaves rather than one box per leaf; and failing both, the
port itself. Every client box just reads `client`: what it was named after is a
guess drawn from an ESI or a port, which would read like an identity it does not
have. Clicking one lists every sub-interface it attaches on, with its service,
vlan and address, and walks to the leaf from there.

Between the leaves and the clients sit the **ethernet-segments**. A multi-homed
client reaches its leaves over one bundle rather than a cable each, and that
bundle is a configured object of its own — it has a name, an ESI every leaf on it
agrees on, and it is where multi-homing goes wrong — so it gets a tier, with the
client hanging off it. A segment box reads `ES` and the last two bytes of its
ESI, which is what tells the segments of a fabric apart; the name each leaf gave
it is in the panel, where two leaves disagreeing on it shows up.

Cables come from LLDP and are de-duplicated: both ends report the same link, and
parallel cables between two nodes are drawn as one line marked `2×`. A link is
coloured by the oper-state of the ports it lands on. Hovering a node pushes back
everything it is not cabled to; clicking one opens its services and its per-port
peer list, and clicking a peer from there walks the fabric.

A drawing is not always one fabric. Nodes that share no cable with each other
are separate topologies, however many clients happen to be plugged into both of
them: a server in two pods says nothing about a path between them, and drawing
them as one claims a crossing that does not exist. So the split is made on the
cables between nodes alone and each fabric gets a **tab** of its own, largest
first, with **All** at the end for everything side by side. A client plugged
into two fabrics is drawn on both tabs, and walking from it to a leaf on the
other one switches tab with it. The tabs are named after whatever tells them
apart — the site their nodes share, the name their nodes share (`frontend-leaf1`
and `frontend-spine1` make a `frontend` tab), or their rank — and a naming that
does not fit every fabric of the drawing is used for none of them, so no tab
reads as a name beside another that reads as a placeholder. Nodes cabled to
nothing at all, including one whose LLDP has not arrived yet, are gathered on an
**Unattached** tab rather than getting one each, and while nothing has any
cables the fabric stays whole.

A fabric wide enough that its node names stop being readable is navigated rather
than read whole: the drawing zooms with the `−` / `+` buttons, with `ctrl` and
the wheel (a trackpad pinch does the same), and with `-`, `+` and `0` on the
keyboard, and it pans by dragging it. **fit** scales the whole fabric down to the
window and follows it as the window and the detail panel change size; the zoom
you pick instead is remembered across reloads. Dragging pans without selecting
what the drag started on, so a node is only opened by a click that stays put.

Neighbours are matched back to the inventory through the name they advertise, so
a containerlab node the inventory calls `clab-dc1-leaf1` is recognized when its
neighbour reports it as `leaf1`. A neighbour that matches no node of the
inventory is still drawn, as an *outside* node, and a node that has not streamed
anything yet is drawn as *unclassified* rather than left out.

## How the live data works

1. When a report is opened for the first time, the server runs its getter once
   against a recording proxy of the gNMI connection. That yields the exact set
   of paths the report reads, so the subscription paths never have to be
   maintained separately from the reports.
2. Each path is bootstrapped with a regular gNMI `Get`, which seeds a per-node
   state tree and pins down the response shape the report getter expects.
3. A gNMI `Subscribe` (STREAM/SAMPLE) then keeps that tree current. Report
   getters run against the tree instead of the device, so a rendered table costs
   no device round-trip at all.
   One tree per node holds every subscription, so reading it back is not simply a
   matter of handing over the subtree: reports overlap (`/interface[name=lag*]`
   and `/interface[name=*]/statistics` both live under `interface`), and SR Linux
   streams whole subtrees for a subscription on one branch of them. What a report
   sees is therefore narrowed back down to what its own path selects — matching
   key predicates, the named branch, and nothing beside it — so a report reads
   what its own `Get` would have returned rather than what its neighbours put
   there.
4. A path that cannot be subscribed to falls back to a short-TTL `Get`, and every
   node is re-read every `--resync` seconds so a missed delete cannot leave a
   stale row behind. Nodes are re-read round-robin rather than all at once, so a
   sweep spreads its `Get`s over the interval.

Step 2 needs data to work with: SR Linux answers a `Get` for a subtree that holds
nothing with an empty response, which does not reveal the shape the report getter
expects. Control-plane driven tables regularly start out that way — no MACs
learned yet, no ES destinations, no IPv6 neighbours, or a spine that has no
bridge table at all. Such a path is *pending* rather than broken: it is left out
of the subscription and served by the short-TTL `Get` of step 4, so the report
renders as empty instead of failing. The first of those `Get`s that comes back
with an entry pins down the shape, and the path joins the subscription from then
on — the table starts streaming by itself, within the `Get` TTL of the first
entry appearing, with no extra round-trip spent on polling for it. `GET
/api/status` marks these paths `pending`, and `streaming` once they are live.

The per-report SAMPLE intervals are tuned per report (5s for interface counters,
60s for system info); `--sample-interval` overrides them all at once.
`GET /api/status` shows what each node is currently subscribed to.

Opening a node's gNMI connection reaches it right away, to fetch its TLS
certificate, so a node that is still booting when the server starts cannot be
connected to yet. Those nodes are retried in the background — at most once every
30 seconds, and only while a report covering them is being rendered — so a
server started alongside the fabric picks each node up as it comes up instead of
reporting it unreachable until restarted. `GET /api/status` lists the nodes that
are still unreachable under `unreachable`.

A node that goes away *after* it was connected — a reboot, or the whole lab being
redeployed — is recovered the same way, at three levels. A `Get` that fails is not
taken as proof that a path cannot be streamed, so the path stays a candidate and
the next `--resync` sweep that gets an answer puts it back on the subscription;
a report that could not be discovered is re-probed rather than written off for
good. Until an answer comes back the table keeps its last known state rather than
blanking, dated by the `generated` and `oldest_update` stamps the API returns, and
the failing `Get` is remembered for the cache TTL so an unreachable node is not
asked again by every report on every refresh.

Losing a node is noticed in three independent ways, because no one of them
catches the others: the `Subscribe` RPC reporting an error, a `Get` failing or
hanging, and updates that were due never arriving. The last one matters more than
it sounds — if the *route* to a node disappears rather than the node refusing
connections, the TCP connection simply falls silent, and with no keepalive on it
gRPC goes on considering the call healthy. Since every path is subscribed in
SAMPLE mode the target reports on a known interval whether anything changed or
not, so updates going missing is the signal. This is what the node pane counts:
`up` means the node is answering, not merely that a connection object exists for
it.

Underneath that, the gRPC channel is given a `max_reconnect_backoff_ms` of 10s,
because the default caps the reconnect backoff at two minutes — long enough that a
node which is briefly gone reads as permanently gone while every call on it fails
fast. As a last resort, a node whose `Get`s have been failing, *or hanging*, for
longer than 30 seconds has its connection replaced outright: a gNMI call carries
no deadline of its own, and a channel belonging to a container that no longer
exists cannot be waited back into life.

## gNMI sessions

SR Linux accepts a limited number of concurrent gRPC sessions per gRPC server —
`/system/grpc-server[name=mgmt]/session-limit`, 20 by default — and that budget
is shared with every other gRPC client of the node. Every in-flight RPC counts,
including a long-running `Subscribe`.

The server is built to stay at **one session per node**: all opened reports share
a single `Subscribe` RPC, and at most one `Get` is in flight per node at a time.
Since gNMI cannot add paths to a running subscription, growing the path set means
replacing the RPC; those restarts are batched, so opening a page full of reports
costs one re-subscribe rather than one per report. Paths that no report has read
for `--idle-timeout` are dropped again, which keeps the streaming load on the
node proportional to what is actually being watched.

`GET /api/status` reports `max_sessions_per_node`, and each node's own view is
available on the device with:

```
❯ info from state /system grpc-server mgmt client *
```

## HTTP API

The UI is a client of a small JSON API, which is just as usable from scripts:

| Endpoint | Description |
| --- | --- |
| `GET /api/reports` | The available reports and their metadata |
| `GET /api/inventory` | Inventory nodes, labels and connection state |
| `GET /api/status` | Per-node subscription state |
| `GET /api/topology` | The fabric graph: nodes with their inferred tier and the fabric they are cabled into, the clients hanging off them, and the links between them |
| `GET /api/report/{name}` | One rendered table as JSON |
| `GET /api/stream/{name}` | The same table, pushed as server-sent events |
| `POST /api/chat` | LLM troubleshooting turn (SSE: `start`, `token`, `tool`, `error`, `done`). Takes an optional `provider` (`openai`, `claude`, `grok`) and `effort`; 503 unless a provider key is set |

Both report endpoints accept `inv_filter=key=value,key=value`; the stream
endpoint also accepts `refresh=<seconds>`.

```
❯ curl -s 'http://localhost:8080/api/report/bgp_peers?inv_filter=role%3Dleaf' | jq '.rows[0]'
```

# MCP Server for AI Agents

`nornir-srl` includes a Model Context Protocol (MCP) server that exposes `fcli` reports as tools for AI agents (like Claude Desktop or Gemini CLI). This allows AI agents to directly query the operational state of your SR Linux fabric.

## Usage

The MCP server is provided by the `fcli-mcp` command. It supports both `stdio` (default) and `http` (SSE) transports.

```bash
# Start with a specific containerlab topology
fcli-mcp --topo-file topo.clab.yml

# Start with a Nornir config file
fcli-mcp --config-file nornir_config.yaml

# Start as an HTTP server
fcli-mcp --transport http --port 8080
```

### Runtime Topology Management

The MCP server can start without an initial topology, allowing you to load or switch topologies at runtime using the following MCP tools:

- `list_topologies`: Discovers available `*.clab.yml` and `nornir_config.yaml` files in a directory.
- `load_topology`: Initializes or switches the active fabric from a containerlab file.
- `load_config`: Initializes or switches the active fabric from a Nornir config file.
- `show_topology`: Displays information about the currently loaded fabric.

## Configuration Examples

### Claude Desktop

Add the following to your `claude_desktop_config.json`:

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

Add the following to your `.gemini/settings.json`:

```json
{
  "mcpServers": {
    "fcli": {
      "command": "fcli-mcp"
    }
  }
}
```


