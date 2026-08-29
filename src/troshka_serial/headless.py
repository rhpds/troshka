"""Headless VM detection for serial-only network OS bootstrap."""

from __future__ import annotations

# Network OS types that use the serial exec API (exec routing, timeouts, etc.).
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

# Only vEOS routes its login prompt to VGA when a display is attached (vrnetlab
# uses qemu -display none).  Junos and IOS-XE use ISO bootstrap and keep VGA.
HEADLESS_SERIAL_EXEC_TYPES = frozenset(
    {
        "eos",
        "arista_eos",
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
    return (serial_exec_type or "").lower() in HEADLESS_SERIAL_EXEC_TYPES
