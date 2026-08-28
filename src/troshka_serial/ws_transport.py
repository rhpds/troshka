"""WebSocket-backed serial transport (KubeVirt virt-launcher console)."""

from __future__ import annotations

import time


class WsSerialTransport:
    def __init__(self, ws, send_fn, read_fn, expect_fn) -> None:
        self.ws = ws
        self._send = send_fn
        self._read = read_fn
        self._expect = expect_fn

    def send(self, text: str) -> None:
        self._send(self.ws, text)

    def read(self, timeout_secs: float) -> str:
        return self._read(self.ws, timeout_secs)

    def expect(self, patterns: list, timeout_secs: float) -> tuple[int | None, str]:
        return self._expect(self.ws, patterns, timeout_secs)

    def poke(self) -> None:
        self._send(self.ws, "\r")
