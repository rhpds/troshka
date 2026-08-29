"""Pexpect-backed serial transport (troshkad / libvirt PTY)."""

from __future__ import annotations


class PexpectSerialTransport:
    def __init__(self, child) -> None:
        self.child = child

    def send(self, text: str) -> None:
        self.child.send(text)

    def read(self, timeout_secs: float) -> str:
        from pexpect import EOF, TIMEOUT

        try:
            return self.child.read_nonblocking(
                size=65536, timeout=max(0.1, timeout_secs)
            )
        except TIMEOUT:
            return ""
        except EOF:
            return ""

    def expect(self, patterns: list, timeout_secs: float) -> tuple[int | None, str]:
        from pexpect import TIMEOUT

        try:
            idx = self.child.expect(patterns, timeout=timeout_secs)
            buf = (self.child.before or "") + (self.child.after or "")
            return idx, buf
        except TIMEOUT:
            return None, self.child.before or ""

    def poke(self) -> None:
        self.child.send("\r")
