"""Tests for comparing two renderings of a report, and for saving one.

The engine works on the table shape both surfaces produce - ``{report, title,
columns, rows, ...}`` - so these tests write that shape out directly.
"""

import json
import time

import pytest

from nornir_srl.diff import (
    ADDED,
    CHANGED,
    REMOVED,
    SAME,
    STATUS,
    diff_nodes,
    diff_rows,
    diff_tables,
    split_by_node,
)
from nornir_srl.reports import REPORTS, get_report
from nornir_srl.server.snapshots import (
    MAX_PER_REPORT,
    SnapshotStore,
    comparable,
    default_directory,
)


def table(rows, columns=("Node", "peer", "state"), report="bgp_peers"):
    return {
        "report": report,
        "title": "BGP Peers",
        "columns": list(columns),
        "rows": [dict(row) for row in rows],
        "errors": [],
        "nodes": len({row.get("Node") for row in rows}),
        "generated": 1700000000.0,
    }


PEER = ("Node", "NI", "peer")


def peers(*rows):
    return table(rows, columns=("Node", "NI", "peer", "state"))


def statuses(result):
    return [row[STATUS] for row in result["rows"]]


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #


def test_two_identical_tables_differ_in_nothing():
    rows = [{"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"}]
    result = diff_tables(peers(*rows), peers(*rows), PEER)
    assert result["rows"] == []
    assert result["diff"]["counts"] == {ADDED: 0, REMOVED: 0, CHANGED: 0, SAME: 1}


def test_a_row_only_the_second_table_has_is_new():
    before = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"})
    after = peers(
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"},
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.2", "state": "established"},
    )
    result = diff_tables(before, after, PEER)
    assert statuses(result) == [ADDED]
    assert result["rows"][0]["peer"] == "10.0.0.2"


def test_a_row_only_the_first_table_has_is_gone():
    before = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"})
    result = diff_tables(before, peers(), PEER)
    assert statuses(result) == [REMOVED]
    assert result["rows"][0]["peer"] == "10.0.0.1"


def test_a_row_the_key_matches_with_a_different_value_has_changed():
    before = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"})
    after = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "idle"})
    result = diff_tables(before, after, PEER)
    assert statuses(result) == [CHANGED]
    assert result["rows"][0]["state"] == "established \u2192 idle"
    assert result["rows"][0]["_changes"] == {"state": ["established", "idle"]}


def test_only_the_cells_that_changed_read_as_changed():
    before = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"})
    after = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "idle"})
    row = diff_tables(before, after, PEER)["rows"][0]
    assert row["peer"] == "10.0.0.1"
    assert list(row["_changes"]) == ["state"]


def test_a_report_with_no_key_reports_a_change_as_a_row_gone_and_a_row_new():
    """Honest: nothing said which of the two rows is the same row."""
    before = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"})
    after = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "idle"})
    result = diff_tables(before, after)
    assert sorted(statuses(result)) == [ADDED, REMOVED]
    assert result["diff"]["keyed"] is False


def test_a_key_naming_no_real_column_falls_back_to_the_whole_row():
    before = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "up"})
    after = peers({"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "down"})
    result = diff_tables(before, after, ("no-such-column",))
    assert sorted(statuses(result)) == [ADDED, REMOVED]
    assert result["diff"]["keyed"] is False


def test_unchanged_rows_are_counted_but_not_shown():
    rows = [{"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"}]
    result = diff_tables(peers(*rows), peers(*rows), PEER)
    assert result["rows"] == []
    assert result["diff"]["counts"][SAME] == 1


def test_unchanged_rows_can_be_asked_for():
    rows = [{"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"}]
    result = diff_tables(peers(*rows), peers(*rows), PEER, include_same=True)
    assert statuses(result) == [SAME]


def test_the_worst_news_comes_first():
    before = peers(
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"},
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.9", "state": "established"},
    )
    after = peers(
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "idle"},
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.5", "state": "established"},
    )
    assert statuses(diff_tables(before, after, PEER)) == [REMOVED, ADDED, CHANGED]


def test_the_verdict_column_comes_first():
    result = diff_tables(peers(), peers(), PEER)
    assert result["columns"][0] == STATUS
    assert result["columns"][1:] == ["Node", "NI", "peer", "state"]


def test_a_column_only_one_table_has_is_still_a_column():
    """A snapshot from an older release, or a node that carries one more field."""
    before = table([{"Node": "leaf1", "peer": "10.0.0.1"}], columns=("Node", "peer"))
    after = table(
        [{"Node": "leaf1", "peer": "10.0.0.1", "state": "up"}],
        columns=("Node", "peer", "state"),
    )
    result = diff_tables(before, after, ("Node", "peer"))
    assert result["columns"] == [STATUS, "Node", "peer", "state"]
    assert statuses(result) == [CHANGED]


def test_rows_sharing_a_key_are_paired_off_rather_than_dropped():
    """A key that does not fully identify a row must not lose the rest."""
    before = peers(
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"},
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"},
    )
    after = peers(
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"},
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "idle"},
    )
    result = diff_tables(before, after, PEER)
    assert statuses(result) == [CHANGED]
    assert result["diff"]["counts"][SAME] == 1


def test_a_missing_value_and_an_empty_one_are_the_same_thing():
    before = table([{"Node": "leaf1", "peer": "10.0.0.1"}])
    after = table([{"Node": "leaf1", "peer": "10.0.0.1", "state": ""}])
    result = diff_tables(before, after, ("Node", "peer"))
    assert result["diff"]["counts"][SAME] == 1


def test_the_labels_name_what_was_compared():
    result = diff_tables(peers(), peers(), PEER, labels=("last good", "now"))
    assert result["diff"]["labels"] == ["last good", "now"]
    assert result["title"].endswith("last good vs now")


def test_the_errors_of_both_tables_are_carried_through():
    before = {**peers(), "errors": [{"node": "leaf9", "error": "was down"}]}
    after = {**peers(), "errors": [{"node": "leaf8", "error": "is down"}]}
    assert [e["node"] for e in diff_tables(before, after, PEER)["errors"]] == [
        "leaf9",
        "leaf8",
    ]


def test_diff_rows_counts_what_it_did():
    rows, counts = diff_rows(
        [{"a": "1"}, {"a": "2"}], [{"a": "2"}, {"a": "3"}], ["a"], ["a"]
    )
    assert counts == {ADDED: 1, REMOVED: 1, CHANGED: 0, SAME: 1}
    assert sorted(row[STATUS] for row in rows) == [ADDED, REMOVED]


# --------------------------------------------------------------------------- #
# one node against another
# --------------------------------------------------------------------------- #


def pair():
    return peers(
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"},
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.2", "state": "established"},
        {"Node": "leaf2", "NI": "default", "peer": "10.0.0.1", "state": "established"},
        {"Node": "leaf2", "NI": "default", "peer": "10.0.0.2", "state": "idle"},
    )


def test_split_by_node_gives_one_table_each():
    per_node = split_by_node(pair())
    assert sorted(per_node) == ["leaf1", "leaf2"]
    assert len(per_node["leaf1"]["rows"]) == 2


def test_two_nodes_of_a_redundant_pair_are_compared_without_their_names():
    """Node is the one column the two are certain to disagree about."""
    result = diff_nodes(pair(), "leaf1", "leaf2", PEER)
    assert "Node" not in result["columns"]
    assert result["diff"]["key_columns"] == ["NI", "peer"]
    assert statuses(result) == [CHANGED]
    assert result["rows"][0]["state"] == "established \u2192 idle"


def test_comparing_two_nodes_says_which_two():
    result = diff_nodes(pair(), "leaf1", "leaf2", PEER)
    assert result["diff"]["labels"] == ["leaf1", "leaf2"]


def test_a_node_with_something_the_other_lacks_reads_as_gone():
    fabric = peers(
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.9", "state": "established"},
        {"Node": "leaf2", "NI": "default", "peer": "10.0.0.1", "state": "established"},
    )
    result = diff_nodes(fabric, "leaf1", "leaf2", PEER)
    assert sorted(statuses(result)) == [ADDED, REMOVED]


def test_a_node_that_is_not_in_the_table_is_said_so_rather_than_read_as_empty():
    result = diff_nodes(pair(), "leaf1", "leaf9", PEER)
    assert [e["node"] for e in result["errors"]] == ["leaf9"]
    assert statuses(result) == [REMOVED, REMOVED]


# --------------------------------------------------------------------------- #
# key columns in the registry
# --------------------------------------------------------------------------- #


def test_a_declared_key_starts_with_the_node_that_holds_the_row():
    """Every row belongs to a node, so nothing else can identify it on its own."""
    for report in REPORTS:
        if report.key_columns:
            assert report.key_columns[0] == "Node", (
                f"{report.name} keys on {report.key_columns} without the node"
            )


def test_the_reports_worth_comparing_declare_a_key():
    for name in (
        "bgp_peers",
        "ipv4_rib",
        "ipv6_rib",
        "mac",
        "arp",
        "nd",
        "lldp",
        "subif",
        "ni",
        "es",
        "vxlan",
    ):
        assert get_report(name).key_columns, f"{name} has no key columns"


def test_key_columns_reach_the_browser():
    assert get_report("bgp_peers").as_dict()["key_columns"] == ["Node", "NI", "peer"]


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path / "snapshots")


def test_a_snapshot_comes_back_as_it_went_in(store):
    original = peers(
        {"Node": "leaf1", "NI": "default", "peer": "10.0.0.1", "state": "established"}
    )
    saved = store.save("bgp_peers", original, label="before the change")
    read = store.get(saved.id)
    assert read is not None
    assert read.table == original
    assert read.label == "before the change"
    assert read.report == "bgp_peers"


def test_a_snapshot_remembers_how_it_was_taken(store):
    saved = store.save(
        "ipv4_rib",
        table([]),
        inv_filter={"role": "leaf"},
        params={"address": "10.0.0.1"},
    )
    read = store.get(saved.id)
    assert read.inv_filter == {"role": "leaf"}
    assert read.params == {"address": "10.0.0.1"}


def test_a_snapshot_records_the_nodes_it_covered(store):
    saved = store.save("bgp_peers", pair())
    assert saved.nodes == ["leaf1", "leaf2"]


def test_snapshots_come_back_newest_first(store):
    for label in ("one", "two", "three"):
        store.save("bgp_peers", table([]), label=label)
        time.sleep(0.01)
    assert [s.label for s in store.list()] == ["three", "two", "one"]


def test_listing_can_be_narrowed_to_one_report(store):
    store.save("bgp_peers", table([]), label="peers")
    store.save("lldp", table([]), label="neighbours")
    assert [s.label for s in store.list("lldp")] == ["neighbours"]


def test_an_empty_directory_lists_nothing(store):
    assert store.list() == []


def test_a_snapshot_can_be_deleted(store):
    saved = store.save("bgp_peers", table([]))
    assert store.delete(saved.id) is True
    assert store.get(saved.id) is None


def test_deleting_something_that_is_not_there_says_so(store):
    assert store.delete("no-such-snapshot") is False


def test_the_oldest_snapshots_of_a_report_are_dropped(store):
    for index in range(MAX_PER_REPORT + 3):
        store.save("bgp_peers", table([]), label=f"n{index}")
        time.sleep(0.005)
    kept = store.list("bgp_peers")
    assert len(kept) == MAX_PER_REPORT
    assert kept[-1].label == "n3"


def test_pruning_one_report_leaves_another_alone(store):
    store.save("lldp", table([]), label="keep me")
    for index in range(MAX_PER_REPORT + 2):
        store.save("bgp_peers", table([]), label=f"n{index}")
        time.sleep(0.005)
    assert [s.label for s in store.list("lldp")] == ["keep me"]


def test_an_unreadable_snapshot_does_not_hide_the_others(store):
    good = store.save("bgp_peers", table([]), label="good")
    (store.directory / "broken.json").write_text("{ not json", encoding="utf-8")
    assert [s.label for s in store.list()] == ["good"]
    assert store.get(good.id) is not None


def test_an_id_cannot_name_a_file_outside_the_directory(store):
    store.save("bgp_peers", table([]))
    assert store.get("../../etc/passwd") is None
    assert store.delete("../../etc/passwd") is False


def test_a_label_cannot_name_a_file_outside_the_directory(store):
    saved = store.save("../../etc/passwd", table([]), label="../../nope")
    assert saved.id.startswith("etc-passwd-")
    assert (store.directory / f"{saved.id}.json").is_file()


def test_a_snapshot_file_is_json_anyone_can_read(store):
    saved = store.save("bgp_peers", table([]), label="readable")
    raw = json.loads((store.directory / f"{saved.id}.json").read_text())
    assert raw["report"] == "bgp_peers"
    assert raw["label"] == "readable"
    assert "table" in raw


def test_the_default_directory_is_under_the_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_directory() == tmp_path / "fcli" / "snapshots"


# --------------------------------------------------------------------------- #
# refusing a comparison that would not mean anything
# --------------------------------------------------------------------------- #


def test_a_snapshot_taken_the_same_way_is_comparable(store):
    saved = store.save("bgp_peers", table([]), inv_filter={"role": "leaf"})
    assert comparable(saved, {"role": "leaf"}, {}) is None


def test_a_snapshot_of_a_different_slice_of_the_fabric_is_not(store):
    """Every node the two do not share would read as one that came or went."""
    saved = store.save("bgp_peers", table([]), inv_filter={"role": "leaf"})
    why = comparable(saved, {}, {})
    assert why and "inventory filter" in why


def test_a_snapshot_taken_with_different_parameters_is_not(store):
    saved = store.save("ipv4_rib", table([]), params={"address": "10.0.0.1"})
    why = comparable(saved, {}, {"address": "10.0.0.2"})
    assert why and "parameters" in why


def test_no_filter_and_an_empty_filter_are_the_same_thing(store):
    saved = store.save("bgp_peers", table([]))
    assert comparable(saved, None, None) is None
    assert comparable(saved, {}, {}) is None


# --------------------------------------------------------------------------- #
# which fabric a snapshot was taken of
#
# One directory holds them all, so without this a snapshot of dc1 compares
# happily against dc2 and reads as every node being replaced at once.
# --------------------------------------------------------------------------- #


def test_a_snapshot_records_the_fabric_it_was_taken_of(store):
    saved = store.save(
        "bgp_peers", table([]), fabric="dc1", inventory=["leaf1", "spine1"]
    )
    assert saved.fabric == "dc1"
    assert saved.inventory == ["leaf1", "spine1"]
    assert store.get(saved.id).fabric == "dc1"


def test_the_inventory_is_kept_sorted_however_it_arrives(store):
    saved = store.save("bgp_peers", table([]), inventory=["spine1", "leaf1"])
    assert saved.inventory == ["leaf1", "spine1"]


def test_the_inventory_is_the_whole_of_it_not_just_the_nodes_with_rows(store):
    """A report has no rows for a node with nothing to report."""
    rows = [{"Node": "leaf1", "peer": "10.0.0.1", "state": "up"}]
    saved = store.save(
        "bgp_peers", table(rows), inventory=["leaf1", "leaf2", "spine1"]
    )
    assert saved.nodes == ["leaf1"]
    assert saved.inventory == ["leaf1", "leaf2", "spine1"]


def test_a_snapshot_of_another_fabric_is_not_comparable(store):
    saved = store.save("bgp_peers", table([]), fabric="dc1")
    why = comparable(saved, {}, {}, fabric="dc2")
    assert why and "dc1" in why and "dc2" in why


def test_a_snapshot_of_this_fabric_is(store):
    saved = store.save("bgp_peers", table([]), fabric="dc1")
    assert comparable(saved, {}, {}, fabric="dc1") is None


def test_an_inventory_sharing_no_node_is_another_fabric(store):
    """Two unnamed inventories, told apart by having nobody in common."""
    saved = store.save("bgp_peers", table([]), inventory=["dc1-leaf1", "dc1-spine1"])
    why = comparable(saved, {}, {}, inventory=["dc2-leaf1", "dc2-spine1"])
    assert why and "share no node" in why


def test_a_fabric_that_grew_a_node_is_still_the_same_fabric(store):
    """Seeing that leaf3 arrived is the entire point of comparing."""
    saved = store.save("bgp_peers", table([]), inventory=["leaf1", "leaf2"])
    assert comparable(saved, {}, {}, inventory=["leaf1", "leaf2", "leaf3"]) is None


def test_a_fabric_that_lost_a_node_is_still_the_same_fabric(store):
    saved = store.save("bgp_peers", table([]), inventory=["leaf1", "leaf2"])
    assert comparable(saved, {}, {}, inventory=["leaf1"]) is None


def test_the_message_does_not_list_a_whole_fabric(store):
    saved = store.save("bgp_peers", table([]), inventory=[f"a{n}" for n in range(9)])
    why = comparable(saved, {}, {}, inventory=["b1"])
    assert "and 6 more" in why


def test_a_snapshot_from_before_the_fabric_was_recorded_still_compares(store):
    """An older snapshot has no fabric; an unknown one is not a different one."""
    saved = store.save("bgp_peers", table([]))
    assert saved.fabric == ""
    assert comparable(saved, {}, {}, fabric="dc1", inventory=["leaf1"]) is None


def test_a_surface_that_names_no_fabric_compares_against_one_that_does(store):
    saved = store.save("bgp_peers", table([]), fabric="dc1", inventory=["leaf1"])
    assert comparable(saved, {}, {}) is None


def test_the_fabric_is_read_back_off_disk(store):
    saved = store.save(
        "bgp_peers", table([]), fabric="dc1", inventory=["leaf1", "spine1"]
    )
    raw = json.loads((store.directory / f"{saved.id}.json").read_text())
    assert raw["fabric"] == "dc1"
    assert raw["inventory"] == ["leaf1", "spine1"]


def test_a_listing_says_which_fabric_each_came_from(store):
    store.save("bgp_peers", table([]), fabric="dc1")
    assert store.list()[0].as_dict()["fabric"] == "dc1"


# --------------------------------------------------------------------------- #
# the CLI over the same engine
#
# These reach the store and the argument guards without contacting a node: the
# inventory is built from a topology file, and nothing here renders a report.
# --------------------------------------------------------------------------- #

TOPOLOGY = """
name: lab1
topology:
  defaults:
    kind: nokia_srlinux
  nodes:
    leaf1:
    leaf2:
"""


@pytest.fixture
def fcli(tmp_path):
    from typer.testing import CliRunner

    from nornir_srl.cli import app

    runner = CliRunner()
    directory = tmp_path / "snapshots"

    def run(*args, lab="lab1"):
        topo = tmp_path / f"{lab}.clab.yml"
        topo.write_text(TOPOLOGY.replace("name: lab1", f"name: {lab}"), encoding="utf-8")
        return runner.invoke(
            app, ["-t", str(topo), *args, "--snapshot-dir", str(directory)]
        )

    run.directory = directory
    return run


def test_the_cli_lists_nothing_when_nothing_is_saved(fcli):
    result = fcli("snapshot", "list")
    assert result.exit_code == 0
    assert "No snapshots" in result.stdout


def test_the_cli_lists_a_snapshot_saved_underneath_it(fcli):
    saved = SnapshotStore(fcli.directory).save(
        "bgp_peers", table([{"Node": "leaf1", "peer": "10.0.0.1", "state": "up"}])
    )
    result = fcli("snapshot", "list")
    assert saved.id in result.stdout
    assert "bgp_peers" in result.stdout


def test_the_cli_narrows_the_list_to_one_report(fcli):
    store = SnapshotStore(fcli.directory)
    store.save("bgp_peers", table([]), label="peers")
    store.save("lldp", table([], report="lldp"), label="neighbours")
    result = fcli("snapshot", "list", "lldp")
    assert "neighbours" in result.stdout
    assert "peers" not in result.stdout


def test_the_cli_writes_the_list_out_as_json_when_asked(fcli):
    SnapshotStore(fcli.directory).save("bgp_peers", table([]), label="peers")
    result = fcli("-o", "json", "snapshot", "list")
    assert json.loads(result.stdout)[0]["label"] == "peers"


def test_the_cli_deletes_a_snapshot(fcli):
    saved = SnapshotStore(fcli.directory).save("bgp_peers", table([]))
    assert fcli("snapshot", "rm", saved.id).exit_code == 0
    assert SnapshotStore(fcli.directory).list() == []


def test_the_cli_says_so_when_there_is_nothing_to_delete(fcli):
    result = fcli("snapshot", "rm", "no-such-snapshot")
    assert result.exit_code == 1


def test_the_cli_refuses_a_report_it_does_not_know(fcli):
    result = fcli("snapshot", "save", "no-such-report")
    assert result.exit_code == 1


def test_the_cli_wants_exactly_one_thing_to_compare_against(fcli):
    assert fcli("diff", "bgp-peers").exit_code == 1
    assert fcli("diff", "bgp-peers", "-a", "x", "-N", "a,b").exit_code == 1


def test_the_cli_takes_a_report_name_with_dashes(fcli):
    """``fcli bgp-peers`` is the command, so ``fcli diff bgp-peers`` should work."""
    result = fcli("diff", "bgp-peers", "--against", "no-such-snapshot")
    assert result.exit_code == 1
    assert "No snapshot" in result.output


def test_the_cli_says_which_fabric_each_snapshot_came_from(fcli):
    SnapshotStore(fcli.directory).save("bgp_peers", table([]), fabric="lab1")
    assert "lab1" in fcli("snapshot", "list").stdout


def test_the_cli_marks_a_snapshot_taken_of_another_fabric(fcli):
    SnapshotStore(fcli.directory).save("bgp_peers", table([]), fabric="lab2")
    assert "other fabric" in fcli("snapshot", "list", lab="lab1").stdout


def test_the_cli_does_not_mark_one_taken_of_this_fabric(fcli):
    SnapshotStore(fcli.directory).save("bgp_peers", table([]), fabric="lab1")
    assert "other fabric" not in fcli("snapshot", "list", lab="lab1").stdout


def test_the_cli_refuses_a_snapshot_of_another_fabric(fcli, monkeypatch):
    from nornir_srl import cli

    monkeypatch.setattr(
        cli, "report_table", lambda *a, **k: pytest.fail("polled the fabric")
    )
    saved = SnapshotStore(fcli.directory).save("bgp_peers", table([]), fabric="lab2")
    result = fcli("diff", "bgp-peers", "--against", saved.id, lab="lab1")
    assert result.exit_code == 1
    assert "Not comparable" in result.output
    assert "lab1" in result.output and "lab2" in result.output


def test_the_cli_settles_the_comparison_before_running_the_fabric(fcli, monkeypatch):
    """Nothing is worth polling every node for if the snapshot is not there."""
    from nornir_srl import cli

    monkeypatch.setattr(
        cli, "report_table", lambda *a, **k: pytest.fail("polled the fabric")
    )
    assert fcli("diff", "bgp-peers", "--against", "no-such-snapshot").exit_code == 1
    assert fcli("diff", "bgp-peers", "--nodes", "leaf1").exit_code == 1

    saved = SnapshotStore(fcli.directory).save("lldp", table([], report="lldp"))
    result = fcli("diff", "bgp-peers", "--against", saved.id)
    assert result.exit_code == 1
    assert "lldp report" in result.output
