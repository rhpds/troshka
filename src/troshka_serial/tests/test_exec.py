"""Tests for shared serial exec dispatch."""

from unittest.mock import patch

from troshka_serial.exec import (
    cap_serial_timeout,
    exec_serial_on_transport,
    serial_method_label,
)


def test_serial_method_label():
    assert serial_method_label("eos") == "serial-eos"
    assert serial_method_label("junos") == "serial-junos"
    assert serial_method_label("linux") == "serial"


def test_cap_serial_timeout_network_os():
    assert cap_serial_timeout("eos", 1200) == 900
    assert cap_serial_timeout("linux", 1200) == 60


def test_exec_serial_on_transport_ios():
    transport = object()

    with patch(
        "troshka_serial.ios.network_serial_exec",
        return_value="Arista EOS",
    ):
        output, exit_code, method = exec_serial_on_transport(
            transport, "eos", "show version", 60, username="admin", password="x"
        )
    assert output == "Arista EOS"
    assert exit_code is None
    assert method == "serial-eos"
