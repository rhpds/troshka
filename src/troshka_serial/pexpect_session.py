"""Pexpect-backed serial sessions shared by troshkad and KubeVirt."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, TypeVar

from troshka_serial.pexpect_transport import PexpectSerialTransport
from troshka_serial.stream_pty import StreamPtyBridge

T = TypeVar("T")


def run_fd_pexpect_session(
    fd: int,
    timeout_secs: float,
    work: Callable[[PexpectSerialTransport], T],
    *,
    close_fd: bool = True,
) -> T:
    """Run *work* with a pexpect fdspawn on *fd*."""
    from pexpect import EOF, TIMEOUT, fdpexpect

    child = fdpexpect.fdspawn(fd, encoding="utf-8", timeout=timeout_secs)
    try:
        return work(PexpectSerialTransport(child))
    except TIMEOUT as exc:
        raise RuntimeError("Command timed out") from exc
    except EOF as exc:
        raise RuntimeError("Console connection closed") from exc
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
        if close_fd:
            try:
                os.close(fd)
            except OSError:
                pass


def run_stream_pexpect_session(
    open_stream: Callable[[], Any],
    timeout_secs: float,
    work: Callable[[PexpectSerialTransport], T],
) -> T:
    """Open a Kubernetes/exec stream, bridge it to pexpect, and run *work*."""
    from pexpect import EOF, TIMEOUT, fdpexpect

    class _StreamBridgedFdSpawn(fdpexpect.fdspawn):
        """fdspawn that sends keystrokes to the exec stream instead of the PTY."""

        def __init__(self, fd: int, stream: Any, **kwargs: Any) -> None:
            super().__init__(fd, **kwargs)
            self._bridge_stream = stream

        def send(self, s: str | bytes) -> int:
            text = self._coerce_send_string(s)
            self._log(text, "send")
            if self._bridge_stream.is_open():
                self._bridge_stream.write_stdin(text)
            return len(text)

    stream = open_stream()
    bridge = StreamPtyBridge(stream)
    try:
        child = _StreamBridgedFdSpawn(
            bridge.fd(), stream, encoding="utf-8", timeout=timeout_secs
        )
        try:
            return work(PexpectSerialTransport(child))
        except TIMEOUT as exc:
            raise RuntimeError("Command timed out") from exc
        except EOF as exc:
            raise RuntimeError("Console connection closed") from exc
        finally:
            try:
                child.close(force=True)
            except Exception:
                pass
    finally:
        bridge.close()
