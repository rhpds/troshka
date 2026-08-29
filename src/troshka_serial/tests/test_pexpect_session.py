"""Tests for stream PTY bridge and pexpect session helpers."""

import fcntl
import os
import threading
import time
from unittest.mock import MagicMock, patch

from troshka_serial.pexpect_session import run_fd_pexpect_session
from troshka_serial.stream_pty import StreamPtyBridge


class _FakeStream:
    def __init__(self, greeting: bytes = b"login:"):
        self._open = True
        self._out = [greeting]
        self._written = []
        self._lock = threading.Lock()

    def is_open(self):
        return self._open

    def update(self, timeout=0):
        return None

    def peek_stdout(self):
        with self._lock:
            return bool(self._out)

    def read_stdout(self):
        with self._lock:
            if not self._out:
                return ""
            return self._out.pop(0)

    def write_stdin(self, data):
        self._written.append(data)

    def close(self):
        self._open = False


def test_stream_pty_bridge_relays_guest_output():
    stream = _FakeStream(b"rtr2 login: ")
    bridge = StreamPtyBridge(stream)
    try:
        flags = fcntl.fcntl(bridge.fd(), fcntl.F_GETFL)
        fcntl.fcntl(bridge.fd(), fcntl.F_SETFL, flags | os.O_NONBLOCK)
        ready = b""
        deadline = time.time() + 2
        while time.time() < deadline and b"login" not in ready:
            try:
                chunk = os.read(bridge.fd(), 4096)
                if chunk:
                    ready += chunk
            except BlockingIOError:
                time.sleep(0.05)
        assert b"login" in ready
    finally:
        bridge.close()


def test_run_fd_pexpect_session_invokes_work():
    transport = MagicMock()

    def work(_transport):
        return "done"

    with patch("pexpect.fdpexpect.fdspawn") as mock_fdspawn:
        child = MagicMock()
        mock_fdspawn.return_value = child
        assert run_fd_pexpect_session(5, 10, work) == "done"
        mock_fdspawn.assert_called_once()
