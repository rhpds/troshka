"""Logging utilities — sanitize user-controlled data before logging."""

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_log(value: object) -> str:
    """Sanitize a value for safe inclusion in log messages.

    Replaces newlines and control characters to prevent log injection (S5145).
    """
    s = str(value)
    s = s.replace("\r\n", "\\r\\n").replace("\n", "\\n").replace("\r", "\\r")
    s = _CONTROL_CHARS.sub("", s)
    return s
