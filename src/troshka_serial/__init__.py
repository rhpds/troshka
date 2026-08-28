"""Shared serial console helpers for network OS and Linux bootstrap."""

from troshka_serial.ios import (
    SERIAL_BLANK_MESSAGE,
    clean_ios_output,
    ios_enable_secret,
    ios_exec_command,
    ios_exec_commands,
    ios_poke_and_login,
    network_serial_exec,
    split_serial_commands,
)
from troshka_serial.junos import (
    junos_clean_output,
    junos_exec_command,
    junos_exec_commands,
    junos_needs_configure_session,
    junos_poke_and_login,
    junos_wrap_command,
    network_junos_serial_exec,
)
from troshka_serial.linux import (
    clean_linux_tempfile_output,
    linux_exec_command,
    linux_poke_and_login,
    linux_serial_exec,
    parse_marker_output,
)
from troshka_serial.exec import (
    cap_serial_timeout,
    exec_serial_on_transport,
    serial_method_label,
)
from troshka_serial.pexpect_session import (
    run_fd_pexpect_session,
    run_stream_pexpect_session,
)

__all__ = [
    "SERIAL_BLANK_MESSAGE",
    "cap_serial_timeout",
    "clean_ios_output",
    "clean_linux_tempfile_output",
    "exec_serial_on_transport",
    "ios_enable_secret",
    "ios_exec_command",
    "ios_exec_commands",
    "ios_poke_and_login",
    "junos_clean_output",
    "junos_exec_command",
    "junos_exec_commands",
    "junos_needs_configure_session",
    "junos_poke_and_login",
    "junos_wrap_command",
    "linux_exec_command",
    "linux_poke_and_login",
    "linux_serial_exec",
    "network_junos_serial_exec",
    "network_serial_exec",
    "parse_marker_output",
    "run_fd_pexpect_session",
    "run_stream_pexpect_session",
    "serial_method_label",
    "split_serial_commands",
]
