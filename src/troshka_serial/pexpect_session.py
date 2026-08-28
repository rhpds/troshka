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
    stream = open_stream()
    bridge = StreamPtyBridge(stream)
    try:
        return run_fd_pexpect_session(
            bridge.fd(), timeout_secs, work, close_fd=False
        )
    finally:
        bridge.close()
