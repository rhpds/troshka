"""Serial console transport protocol used by host agent and KubeVirt backends."""

from __future__ import annotations

from typing import Protocol


class SerialTransport(Protocol):
    """Minimal read/send/expect API for network-OS serial sessions."""

    def send(self, text: str) -> None:
        """Send raw bytes/text to the serial console."""

    def read(self, timeout_secs: float) -> str:
        """Read available console output (may return partial data)."""

    def expect(
        self, patterns: list, timeout_secs: float
    ) -> tuple[int | None, str]:
        """Block until one pattern matches. Returns (index, buffer) or (None, buffer)."""

    def poke(self) -> None:
        """Send a wake-up character (usually CR)."""
