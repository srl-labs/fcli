"""Turning a containerlab topology file into a Nornir inventory.

Both the CLI and the MCP server take a ``.clab.yml`` and have to work out which
of its nodes are SR Linux. Keeping that in one place is what stops the two from
drifting apart.
"""

from typing import Any, Dict, List, Optional

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


def srl_kinds(topology: Dict[str, Any]) -> List[str]:
    """Every kind in *topology* that denotes an SR Linux node."""
    kinds = _mapping(topology, "kinds")
    found = [
        kind
        for kind in kinds
        if "/srlinux" in (_mapping(kinds, kind).get("image") or "")
    ]
    for reserved in SRL_KINDS:
        if reserved not in found:
            found.append(reserved)
    return found


def _srlinux_by_default(topology: Dict[str, Any]) -> bool:
    """Whether a node that names no kind of its own is an SR Linux node."""
    defaults = _mapping(topology, "defaults")
    default_kind = defaults.get("kind")
    if default_kind in SRL_KINDS:
        return True
    # A default image can be pinned directly or inherited from the default
    # kind's entry under 'kinds', and either one settles it on its own.
    default_image = defaults.get("image") or _mapping(
        _mapping(topology, "kinds"), default_kind
    ).get("image")
    return bool(default_image and "srlinux" in default_image)


def srl_hosts(topo: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The SR Linux nodes of *topo*, as a Nornir hosts inventory."""
    topology = _mapping(topo, "topology")
    prefix = node_prefix(topo)
    kinds = srl_kinds(topology)
    by_default = _srlinux_by_default(topology)

    nodes = _mapping(topology, "nodes")
    hosts: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        node_spec = _mapping(nodes, node)
        node_kind = node_spec.get("kind")
        if (node_kind is None and by_default) or node_kind in kinds:
            name = f"{prefix}{node}"
            hosts[name] = {
                "hostname": name,
                "platform": "srlinux",
                "groups": ["srl"],
                "data": _mapping(node_spec, "labels"),
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
