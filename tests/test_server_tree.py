"""Unit tests for the gNMI state tree backing the fcli server."""

from nornir_srl.server.tree import (
    ListNode,
    delete,
    get_node,
    insert,
    join_path,
    materialize,
    parse_elem,
    parse_path,
    split_path,
    strip_module,
)

# --------------------------------------------------------------------------- #
# path parsing
# --------------------------------------------------------------------------- #


def test_split_path_ignores_slashes_inside_keys():
    assert split_path("/interface[name=ethernet-1/1]/statistics") == [
        "interface[name=ethernet-1/1]",
        "statistics",
    ]


def test_split_path_strips_empty_components():
    assert split_path("//system/lldp/") == ["system", "lldp"]


def test_parse_elem_without_keys():
    assert parse_elem("statistics") == ("statistics", {})


def test_parse_elem_with_multiple_keys():
    name, keys = parse_elem("route[prefix=10.0.0.0/24][owner=bgp]")
    assert name == "route"
    assert keys == {"prefix": "10.0.0.0/24", "owner": "bgp"}


def test_parse_elem_strips_module_prefix():
    assert parse_elem("srl_nokia-interfaces:interface[name=mgmt0]") == (
        "interface",
        {"name": "mgmt0"},
    )


def test_parse_path_full():
    assert parse_path("/network-instance[name=default]/protocols/bgp") == [
        ("network-instance", {"name": "default"}),
        ("protocols", {}),
        ("bgp", {}),
    ]


def test_join_path_drops_empty_parts():
    assert join_path("", "system/lldp", None, "interface") == "system/lldp/interface"


def test_strip_module():
    assert strip_module("srl_nokia-if:interface") == "interface"
    assert strip_module("interface") == "interface"


# --------------------------------------------------------------------------- #
# tree mutation
# --------------------------------------------------------------------------- #


def test_insert_scalar_leaf():
    tree = {}
    insert(tree, "system/information/version", "24.10.1")
    assert materialize(tree) == {"system": {"information": {"version": "24.10.1"}}}


def test_insert_keyed_element_renders_as_list():
    tree = {}
    insert(tree, "interface[name=ethernet-1/1]/admin-state", "enable")
    insert(tree, "interface[name=ethernet-1/2]/admin-state", "disable")
    assert materialize(tree) == {
        "interface": [
            {"name": "ethernet-1/1", "admin-state": "enable"},
            {"name": "ethernet-1/2", "admin-state": "disable"},
        ]
    }


def test_insert_merges_subtree_then_leaf():
    tree = {}
    insert(tree, "system/lldp", {"interface": [{"name": "e1", "enabled": True}]})
    insert(tree, "system/lldp/interface[name=e1]/enabled", False)
    assert materialize(tree) == {
        "system": {"lldp": {"interface": [{"name": "e1", "enabled": False}]}}
    }


def test_insert_keeps_entries_without_key_leaves():
    tree = {}
    insert(tree, "system/lldp", {"neighbor": [{"system-name": "leaf2"}]})
    insert(tree, "system/lldp/neighbor[id=1]/system-name", "spine1")
    neighbors = materialize(tree)["system"]["lldp"]["neighbor"]
    assert {"system-name": "leaf2"} in neighbors
    assert {"id": "1", "system-name": "spine1"} in neighbors


def test_insert_strips_module_prefixes_from_values():
    tree = {}
    insert(tree, "interface[name=e1]", {"srl_nokia-if:type": "srl_nokia-if:bridged"})
    assert materialize(tree) == {"interface": [{"name": "e1", "type": "bridged"}]}


def test_insert_at_root_merges():
    tree = {"a": 1}
    insert(tree, "", {"b": 2})
    assert materialize(tree) == {"a": 1, "b": 2}


def test_later_subtree_update_replaces_list_entry_content():
    tree = {}
    insert(tree, "interface[name=e1]/statistics", {"in-octets": 10, "out-octets": 20})
    insert(tree, "interface[name=e1]/statistics", {"in-octets": 30})
    stats = materialize(tree)["interface"][0]["statistics"]
    assert stats == {"in-octets": 30, "out-octets": 20}


def test_delete_removes_list_entry():
    tree = {}
    insert(tree, "interface[name=e1]/admin-state", "enable")
    insert(tree, "interface[name=e2]/admin-state", "enable")
    delete(tree, "interface[name=e1]")
    assert materialize(tree) == {"interface": [{"name": "e2", "admin-state": "enable"}]}


def test_delete_last_list_entry_drops_the_list():
    tree = {}
    insert(tree, "interface[name=e1]/admin-state", "enable")
    delete(tree, "interface[name=e1]")
    assert materialize(tree) == {}


def test_delete_container():
    tree = {}
    insert(tree, "system/lldp/enabled", True)
    delete(tree, "system/lldp")
    assert materialize(tree) == {"system": {}}


def test_delete_unknown_path_is_a_noop():
    tree = {}
    insert(tree, "system/lldp/enabled", True)
    delete(tree, "system/bgp/nope")
    assert materialize(tree) == {"system": {"lldp": {"enabled": True}}}


def test_get_node_returns_list_entry():
    tree = {}
    insert(tree, "interface[name=ethernet-1/1]/statistics/in-octets", 42)
    node = get_node(tree, "interface[name=ethernet-1/1]/statistics")
    assert materialize(node) == {"in-octets": 42}


def test_get_node_missing_returns_none():
    assert get_node({}, "interface[name=e1]") is None


def test_materialize_deep_copies_opaque_values():
    tree = {}
    insert(tree, "system", {"routes": [1, 2, 3]})
    snapshot = materialize(tree)
    snapshot["system"]["routes"].append(4)
    assert materialize(tree)["system"]["routes"] == [1, 2, 3]


def test_list_node_len_tracks_entries():
    node = ListNode()
    node.entry({"name": "e1"})
    node.entry({"name": "e2"})
    node.entry({"name": "e1"})
    assert len(node) == 2
