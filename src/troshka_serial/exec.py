"""Shared serial-exec dispatch for troshkad and KubeVirt backends."""

from __future__ import annotations

_NETWORK_SERIAL_TYPES = frozenset(
    {
        "ios",
        "iosxe",
        "cisco_iosxe",
        "eos",
        "arista_eos",
        "junos",
        "juniper_junos",
    }
)


def serial_method_label(serial_type: str) -> str:
    """Return the exec method label for API responses."""
    st = (serial_type or "linux").lower()
    if st in ("junos", "juniper_junos"):
        return "serial-junos"
    if st in ("eos", "arista_eos"):
        return "serial-eos"
    if st in ("ios", "iosxe", "cisco_iosxe"):
        return "serial-ios"
    return "serial"


def cap_serial_timeout(serial_type: str, requested: int) -> int:
    """Cap exec timeout — network OS config pushes need longer sessions."""
    st = (serial_type or "linux").lower()
    max_timeout = 900 if st in _NETWORK_SERIAL_TYPES else 60
    return min(int(requested), max_timeout)


def exec_serial_on_transport(
    transport,
    serial_type: str,
    command: str,
    timeout: int,
    username: str = "root",
    password: str = "",
) -> tuple[str, int | None, str]:
    """Run *command* on *transport*. Returns (output, exit_code, method_label).

    Raises RuntimeError when login or command execution fails.
    """
    from troshka_serial.ios import network_serial_exec
    from troshka_serial.junos import network_junos_serial_exec
    from troshka_serial.linux import linux_serial_exec

    st = (serial_type or "linux").lower()
    timeout_secs = cap_serial_timeout(st, timeout)

    if st in ("junos", "juniper_junos"):
        output = network_junos_serial_exec(transport, command, timeout_secs)
        return output, None, serial_method_label(st)

    if st in ("ios", "iosxe", "cisco_iosxe", "eos", "arista_eos"):
        user = username or "admin"
        output = network_serial_exec(
            transport, command, timeout_secs, user, password or ""
        )
        return output, None, serial_method_label(st)

    output, exit_code = linux_serial_exec(
        transport,
        command,
        timeout_secs,
        username or "root",
        password or "",
    )
    return output, exit_code, serial_method_label(st)
