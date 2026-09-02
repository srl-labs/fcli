"""Turning a containerlab topology file into a Nornir inventory.

Both the CLI and the MCP server take a ``.clab.yml`` and have to work out which
of its nodes are SR Linux. Keeping that in one place is what stops the two from
drifting apart.
"""

from typing import Any, Dict, Optional

SRL_DEFAULT_USERNAME = "admin"
SRL_DEFAULT_PASSWORD = "NokiaSrl1!"
SRL_DEFAULT_GNMI_PORT = 57400

#: Kinds containerlab reserves for SR Linux. They identify a node on their own,
#: whether or not the topology also pins an image.
SRL_KINDS = ("srl", "nokia_srlinux")

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


def _mapping(parent: Any, key: Any) -> Dict[str, Any]:
    """``parent[key]`` as a mapping, treating absent and empty YAML alike.

    A key written without a value - ``defaults:`` on a line of its own, or a
    node listed as bare ``leaf1:`` - parses as ``None``, which a plain
    ``.get(key, {})`` would hand straight to the next lookup.
    """
    if not isinstance(parent, dict):
        return {}
    value = parent.get(key)
    return value if isinstance(value, dict) else {}


def node_prefix(topo: Dict[str, Any]) -> str:
    """The name containerlab prepends to every node in *topo*."""
    lab_name = topo["name"]
    if "prefix" not in topo:
        return f"clab-{lab_name}-"
    prefix = topo["prefix"]
    if prefix == "__lab-name":
        return f"{lab_name}-"
    if prefix == "":
        return ""
    return f"{prefix}-{lab_name}-"


def _inherited(topology: Dict[str, Any], node_spec: Dict[str, Any], key: str) -> Any:
    """The value of *key* for a node, down containerlab's inheritance chain.

    A node's own setting wins, then the group it is in, then the topology
    defaults. ``kinds`` is not a link in that chain: it is keyed by the kind
    the chain settles on, so it can only be read once that kind is known.
    """
    group = _mapping(_mapping(topology, "groups"), node_spec.get("group"))
    for source in (node_spec, group, _mapping(topology, "defaults")):
        value = source.get(key)
        if value:
            return value
    return None


def is_srl_node(topology: Dict[str, Any], node_spec: Dict[str, Any]) -> bool:
    """Whether the node described by *node_spec* is an SR Linux node.

    The kind the node inherits settles it, and for a kind whose name
    containerlab does not reserve, the image behind that kind does.
    """
    kind = _inherited(topology, node_spec, "kind")
    if kind in SRL_KINDS:
        return True
    kind_image = _mapping(_mapping(topology, "kinds"), kind).get("image")
    return "srlinux" in (kind_image or _inherited(topology, node_spec, "image") or "")


def _labels(topology: Dict[str, Any], node_spec: Dict[str, Any]) -> Dict[str, Any]:
    """A node's labels, with the ones it inherits merged in underneath."""
    group = _mapping(_mapping(topology, "groups"), node_spec.get("group"))
    labels = dict(_mapping(_mapping(topology, "defaults"), "labels"))
    labels.update(_mapping(group, "labels"))
    labels.update(_mapping(node_spec, "labels"))
    return labels


def srl_hosts(topo: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The SR Linux nodes of *topo*, as a Nornir hosts inventory."""
    topology = _mapping(topo, "topology")
    prefix = node_prefix(topo)

    nodes = _mapping(topology, "nodes")
    hosts: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        node_spec = _mapping(nodes, node)
        if not is_srl_node(topology, node_spec):
            continue
        name = f"{prefix}{node}"
        hosts[name] = {
            "hostname": name,
            "platform": "srlinux",
            "groups": ["srl"],
            "data": _labels(topology, node_spec),
        }
    return hosts


def srl_groups(
    gnmi_port: int = SRL_DEFAULT_GNMI_PORT,
    cert_file: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """The Nornir groups inventory the hosts from :func:`srl_hosts` belong to."""
    extras: Dict[str, Any] = {}
    if cert_file:
        extras["path_cert"] = str(cert_file)
    return {
        "srl": {
            "connection_options": {
                "srlinux": {
                    "username": SRL_DEFAULT_USERNAME,
                    "password": SRL_DEFAULT_PASSWORD,
                    "port": gnmi_port,
                    "extras": extras,
                }
            }
        }
    }
