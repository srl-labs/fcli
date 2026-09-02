"""Read-only MD-CLI / gNMI path guards for the in-server agent."""

import pytest

from nornir_srl.server.cli_guard import CliRejected, check_cli, check_gnmi_path


@pytest.mark.parametrize(
    "command",
    [
        "show version",
        "info from state interface ethernet-1/1",
        "info from running /system",
        "tree /network-instance",
        "show routing-policy prefix-set foo",
    ],
)
def test_check_cli_allows_read_only(command):
    assert check_cli(command) == command


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "set /interface ethernet-1/1",
        "enter candidate",
        "commit stay",
        "bash",
        "show version | bash",
        "show version; set interface ethernet-1/1",
        "info from state\nset interface ethernet-1/1",
        "show version && save",
        "delete /interface ethernet-1/1",
    ],
)
def test_check_cli_rejects_writes_and_chaining(command):
    with pytest.raises(CliRejected):
        check_cli(command)


def test_check_gnmi_path_requires_slash():
    with pytest.raises(CliRejected):
        check_gnmi_path("interface ethernet-1/1")


def test_check_gnmi_path_rejects_cli_origin():
    with pytest.raises(CliRejected):
        check_gnmi_path("cli:/show version")
    with pytest.raises(CliRejected):
        check_gnmi_path("/cli/show version")


def test_check_gnmi_path_accepts_yang():
    assert check_gnmi_path("/interface[name=ethernet-1/1]") == (
        "/interface[name=ethernet-1/1]"
    )
