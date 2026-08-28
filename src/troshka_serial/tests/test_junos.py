"""Tests for shared Junos serial helpers."""

from troshka_serial.junos import (
    junos_clean_output,
    junos_needs_configure_session,
    junos_wrap_command,
)


def test_junos_wrap_command_adds_cli_prefix():
    assert junos_wrap_command("show version") == "cli show version"
    assert junos_wrap_command("cli show route") == "cli show route"
    assert junos_wrap_command("") == ""


def test_junos_clean_output_strips_shell_prompt():
    raw = "cli show version\r\nHostname: rtr3\r\nroot@rtr3:~ #\r\n"
    out = junos_clean_output(raw, "cli show version")
    assert "Hostname: rtr3" in out
    assert "root@" not in out


def test_junos_needs_configure_session():
    assert junos_needs_configure_session(["configure", "set system host-name rtr3"])
