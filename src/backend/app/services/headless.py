"""Headless VM helpers (shared with troshkad via troshka_serial)."""

from __future__ import annotations

import os
import sys

_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from troshka_serial.headless import (  # noqa: E402
    NETWORK_SERIAL_EXEC_TYPES,
    serial_exec_needs_headless,
)

__all__ = ["NETWORK_SERIAL_EXEC_TYPES", "serial_exec_needs_headless"]
