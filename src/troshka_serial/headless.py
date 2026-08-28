"""Headless VM detection for serial-only network OS bootstrap."""

from __future__ import annotations

NETWORK_SERIAL_EXEC_TYPES = frozenset(
    {
        "eos",
        "arista_eos",
        "ios",
        "iosxe",
        "cisco_iosxe",
        "junos",
        "juniper_junos",
    }
)


def serial_exec_needs_headless(
    *,
    headless: bool | None = None,
    serial_exec_type: str = "",
) -> bool:
    """Return True when the VM should run without a graphical display."""
    if headless is not None:
        return bool(headless)
    return (serial_exec_type or "").lower() in NETWORK_SERIAL_EXEC_TYPES
