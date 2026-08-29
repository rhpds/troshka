"""Bridge a bidirectional byte stream to a PTY for pexpect."""

from __future__ import annotations

import os
import threading
import time


class StreamPtyBridge:
    """Relay guest serial I/O through a PTY master fd for fdpexpect."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._master, self._slave = os.openpty()
        self._stop = threading.Event()
        self._stdout_thread = threading.Thread(
            target=self._stdout_to_pty, name="serial-stdout", daemon=True
        )
        self._stdout_thread.start()

    def fd(self) -> int:
        return self._master

    def _stdout_to_pty(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._stream.is_open():
                    break
                self._stream.update(timeout=0.5)
                if not self._stream.peek_stdout():
                    time.sleep(0.05)
                    continue
                chunk = self._stream.read_stdout()
                if not chunk:
                    continue
                data = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                os.write(self._slave, data)
            except Exception:
                break
        self._stop.set()

    def close(self) -> None:
        self._stop.set()
        try:
            self._stream.close()
        except Exception:
            pass
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass
