"""Every report, replayed against every SR Linux release we have recorded.

The getters hard-code gNMI paths and the YANG structure they expect back, and
both move between releases. A unit test with hand-written payloads cannot catch
that, because the payload is whatever the test author believed the device sends.

So the fixtures under ``tests/fixtures/releases/`` are the real thing: the gNMI
exchange of every report against a configured fabric, recorded once per release
by ``tests/system/capture.py``. Replaying them here runs the production getters
over real payloads from four releases without a lab, and asserts that

* no report raises on any release,
* the paths a release rejects are the ones we know about, and
* each report still produces the table it produced on the device.

See ``tests/fixtures/releases/MATRIX.md`` for what the lab covers and how to
re-record it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

import pytest

from nornir_srl.reports import REPORTS_BY_NAME
from tests.system.capture import SKIP
from tests.system.replay import Recording, recording_paths

#: gNMI paths a release does not have, and the report that asks for them.
#:
#: A report is allowed to ask for a path the device rejects - it has to, to
#: support several releases from one code base - as long as it handles the
#: rejection instead of failing. What this pins down is *which* paths that
#: applies to, so that a path newly rejected by a release, or one that quietly
#: started working, shows up as a failure here rather than as an empty report.
#: The two l3vpn variants, rejected by every release the lab covers. SR Linux
#: answers with ``Path not valid - unknown element 'l3vpn-ipv4-unicast'``, a
#: schema error rather than absent data: this fabric is EVPN-VXLAN, and the
#: ``bgp-rib`` model only carries the l3vpn containers on a node configured for
#: MPLS IP-VPN. The reports degrade to an empty table, which is why they are
#: listed here rather than fixed.
_L3VPN_MISSING = {
    (
        "bgp_rib_l3vpn_v4",
        "/network-instance[name=*]/bgp-rib/afi-safi[afi-safi-name=l3vpn-ipv4-unicast]"
        "/l3vpn-ipv4-unicast/local-rib/route",
    ),
    (
        "bgp_rib_l3vpn_v6",
        "/network-instance[name=*]/bgp-rib/afi-safi[afi-safi-name=l3vpn-ipv6-unicast]"
        "/l3vpn-ipv6-unicast/local-rib/route",
    ),
}

EXPECTED_MISSING_PATHS: Dict[str, Set[Tuple[str, str]]] = {
    "25.3.2": set(_L3VPN_MISSING),
    "25.10.3": set(_L3VPN_MISSING),
    "26.3.1": set(_L3VPN_MISSING),
    "26.7.1": set(_L3VPN_MISSING),
}


@lru_cache(maxsize=None)
def _recording(path: str) -> Recording:
    return Recording.load(Path(path))


def _cases() -> Iterator[pytest.param]:
    """One case per (release, node, report) recorded."""
    for path in recording_paths():
        recording = _recording(str(path))
        node = recording.node.rsplit("-", 1)[-1]
        for name in recording.reports:
            yield pytest.param(
                str(path),
                name,
                id=f"{recording.release}-{node}-{name}",
            )


CASES = list(_cases())


def _releases() -> List[str]:
    return sorted({_recording(str(p)).release for p in recording_paths()})


def test_fixtures_exist() -> None:
    """Without recordings the rest of this module would silently pass."""
    assert recording_paths(), (
        "no release recordings found under tests/fixtures/releases/ - "
        "capture them with tests/system/capture.py against a live lab"
    )


def test_fixtures_cover_the_report_registry() -> None:
    """Every report is exercised on every release we claim to support."""
    expected = {name for name in REPORTS_BY_NAME if name not in SKIP}
    for path in recording_paths():
        recording = _recording(str(path))
        missing = expected - set(recording.reports)
        assert not missing, (
            f"{recording.release}/{recording.node} has no recording for "
            f"{sorted(missing)} - re-record it with "
            f"python -m tests.system.capture --release {recording.release} "
            f"--node {recording.node}"
        )


@pytest.mark.parametrize("path,report", CASES)
def test_report_does_not_fail_on_release(path: str, report: str) -> None:
    """A report must never raise, whatever the release answers."""
    recording = _recording(path)
    captured = recording.reports[report]
    assert captured.getter_error is None, (
        f"{report} raised on {recording.release}/{recording.node}: "
        f"{captured.getter_error}"
    )
    recording.run(report)


@pytest.mark.parametrize("path,report", CASES)
def test_report_reproduces_the_table_the_device_gave(path: str, report: str) -> None:
    """Replaying the recording yields the report the live device produced.

    This is what turns a datamodel change into a test failure: the payload is
    fixed at what the release actually sent, so any drift in how a getter reads
    it shows up as a differing table.
    """
    recording = _recording(path)
    captured = recording.reports[report]

    columns, rows = recording.table(report)

    assert columns == captured.columns, (
        f"{report} on {recording.release}/{recording.node}: columns changed"
    )
    assert rows == captured.rows, (
        f"{report} on {recording.release}/{recording.node}: "
        f"{len(rows)} rows replayed, {len(captured.rows)} recorded"
    )


@pytest.mark.parametrize("release", _releases())
def test_only_known_paths_are_rejected(release: str) -> None:
    """The paths a release rejects are the ones documented above."""
    rejected: Set[Tuple[str, str]] = set()
    for path in recording_paths():
        recording = _recording(str(path))
        if recording.release != release:
            continue
        for report in recording.reports.values():
            for call in report.failed_paths:
                rejected.add((report.report, call.path))

    expected = EXPECTED_MISSING_PATHS.get(release, set())
    assert rejected == expected, (
        f"{release}: rejected paths changed.\n"
        f"  newly rejected: {sorted(rejected - expected)}\n"
        f"  no longer rejected: {sorted(expected - rejected)}\n"
        "Update EXPECTED_MISSING_PATHS once you have confirmed the change is real."
    )


@pytest.mark.parametrize("release", _releases())
def test_reports_that_need_learned_state_have_rows_on_a_leaf(release: str) -> None:
    """The state-dependent reports are not silently empty on any release.

    ``mac``, ``arp``, ``es`` and friends read learned state, so an empty result
    proves nothing about the paths behind them. The capture runs traffic through
    the lab services first; if one of these comes back empty on a release, either
    the recording was taken too early or the release moved the path.
    """
    needs_state = ["mac", "arp", "es", "es_dest", "irb", "vxlan", "lag", "lldp"]
    for path in recording_paths():
        recording = _recording(str(path))
        if recording.release != release or recording.role != "leaf":
            continue
        empty = [r for r in needs_state if not recording.reports[r].rows]
        assert not empty, (
            f"{release}/{recording.node}: {empty} recorded no rows on a leaf "
            "that has services, LAGs and traffic"
        )
