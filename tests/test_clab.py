"""Tests for turning a containerlab topology into a Nornir inventory."""

import yaml

from nornir_srl import clab


def topo(text: str):
    return yaml.safe_load(text)


# --------------------------------------------------------------------------- #
# node prefix
# --------------------------------------------------------------------------- #


def test_prefix_defaults_to_clab_labname():
    assert clab.node_prefix(topo("name: lab1")) == "clab-lab1-"


def test_prefix_lab_name_keyword_drops_the_clab_part():
    assert clab.node_prefix(topo("name: lab1\nprefix: __lab-name")) == "lab1-"


def test_empty_prefix_leaves_node_names_alone():
    assert clab.node_prefix(topo('name: lab1\nprefix: ""')) == ""


def test_custom_prefix_keeps_the_lab_name():
    assert clab.node_prefix(topo("name: lab1\nprefix: acme")) == "acme-lab1-"


# --------------------------------------------------------------------------- #
# which nodes are SR Linux
# --------------------------------------------------------------------------- #


def test_kind_under_defaults_covers_nodes_that_name_none():
    """The idiomatic topology: everything inherited, nodes written bare.

    A node listed as ``leaf1:`` with nothing under it parses as None, which
    used to be handed straight to .get().
    """
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              defaults:
                kind: nokia_srlinux
                image: ghcr.io/nokia/srlinux:24.7.1
              nodes:
                leaf1:
                leaf2:
            """
        )
    )
    assert sorted(hosts) == ["clab-lab1-leaf1", "clab-lab1-leaf2"]


def test_default_kind_without_a_kinds_section():
    """'kinds' is optional - an image pinned per node is enough for clab."""
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              defaults:
                kind: nokia_srlinux
              nodes:
                leaf1:
                  image: ghcr.io/nokia/srlinux:24.7.1
            """
        )
    )
    assert sorted(hosts) == ["clab-lab1-leaf1"]


def test_kind_entry_carrying_no_image():
    """A 'kinds' entry may set type or binds and leave the image to the node."""
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              kinds:
                nokia_srlinux:
                  type: ixrd3
              nodes:
                leaf1:
                  kind: nokia_srlinux
            """
        )
    )
    assert sorted(hosts) == ["clab-lab1-leaf1"]


def test_default_image_counts_without_a_default_kind():
    """The bug from issue #23: 'A or B if kind else None' dropped A.

    Precedence made the whole 'or' conditional on a default kind being set, so
    an image pinned directly under defaults was thrown away.
    """
    topology = topo(
        """
        name: lab1
        topology:
          defaults:
            image: ghcr.io/nokia/srlinux:24.7.1
          nodes:
            leaf1: {}
        """
    )
    assert sorted(clab.srl_hosts(topology)) == ["clab-lab1-leaf1"]


def test_image_under_kinds_identifies_a_custom_kind_name():
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              kinds:
                my-srl:
                  image: ghcr.io/nokia/srlinux:24.7.1
              nodes:
                leaf1:
                  kind: my-srl
            """
        )
    )
    assert sorted(hosts) == ["clab-lab1-leaf1"]


def test_nodes_of_other_kinds_stay_out_of_the_inventory():
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              kinds:
                nokia_srlinux:
                  image: ghcr.io/nokia/srlinux:24.7.1
                linux:
                  image: ghcr.io/srl-labs/network-multitool
              nodes:
                leaf1:
                  kind: nokia_srlinux
                client1:
                  kind: linux
            """
        )
    )
    assert sorted(hosts) == ["clab-lab1-leaf1"]


def test_a_bare_node_is_not_srlinux_without_a_default_saying_so():
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              nodes:
                leaf1:
            """
        )
    )
    assert hosts == {}


def test_empty_sections_are_read_as_absent():
    """Every clab section can be written as a key with nothing under it."""
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              defaults:
              kinds:
              nodes:
                leaf1:
                  kind: srl
                  labels:
            """
        )
    )
    assert hosts["clab-lab1-leaf1"]["data"] == {}


def test_labels_are_carried_over_as_host_data():
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              nodes:
                leaf1:
                  kind: nokia_srlinux
                  labels:
                    role: leaf
                    site: dc1
            """
        )
    )
    assert hosts["clab-lab1-leaf1"]["data"] == {"role": "leaf", "site": "dc1"}


def test_hosts_carry_the_prefixed_name_and_platform():
    hosts = clab.srl_hosts(
        topo(
            """
            name: lab1
            topology:
              nodes:
                leaf1:
                  kind: srl
            """
        )
    )
    assert hosts["clab-lab1-leaf1"] == {
        "hostname": "clab-lab1-leaf1",
        "platform": "srlinux",
        "groups": ["srl"],
        "data": {},
    }


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #


def test_groups_carry_the_gnmi_port():
    options = clab.srl_groups(57410)["srl"]["connection_options"]["srlinux"]
    assert options["port"] == 57410
    assert options["extras"] == {}


def test_a_cert_file_is_passed_to_the_connection():
    options = clab.srl_groups(57400, "/tmp/ca.pem")["srl"]["connection_options"][
        "srlinux"
    ]
    assert options["extras"]["path_cert"] == "/tmp/ca.pem"
