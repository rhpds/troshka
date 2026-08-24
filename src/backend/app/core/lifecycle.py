"""Audit log + heartbeat for debugging abrupt backend process deaths."""

from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
import time
from pathlib import Path

RUNTIME_DIR = Path.home() / ".cache" / "troshka"
LOG_PATH = RUNTIME_DIR / "lifecycle.log"
logger = logging.getLogger(__name__)


def _ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        RUNTIME_DIR.chmod(0o700)
    except OSError:
        pass


_started_at = time.monotonic()
_handlers_installed = False


def audit(message: str) -> None:
    """Append a timestamped line to the lifecycle log and mirror to app logger."""
    line = (
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
        f"uptime={time.monotonic() - _started_at:.1f}s {message}"
    )
    try:
        _ensure_runtime_dir()
        with LOG_PATH.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    logger.info("lifecycle: %s", message)


def install_signal_audit() -> None:
    """Log incoming termination signals without replacing uvicorn's handlers."""
    global _handlers_installed
    if _handlers_installed:
        return
    _handlers_installed = True

    from app.core.crash_report import record_signal

    def _wrap(_signum: int, handler):
        if handler is None or handler in (signal.SIG_DFL, signal.SIG_IGN):
            return handler

        def wrapped(sig, frame):
            record_signal(sig, frame)
            audit(f"signal {signal.Signals(sig).name} received")
            handler(sig, frame)

        return wrapped

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            old = signal.getsignal(signum)
            signal.signal(signum, _wrap(signum, old))
        except (OSError, ValueError):
            pass

    atexit.register(lambda: audit("atexit handler running"))


def start_heartbeat(interval: int = 30) -> None:
    """Log periodic heartbeats so abrupt deaths have a last-alive timestamp."""

    def _loop() -> None:
        # First beat soon after startup — don't wait a full interval
        time.sleep(min(10, interval))
        while True:
            audit("heartbeat")
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="lifecycle-heartbeat").start()


def run_startup_step(name: str, fn) -> None:
    """Time a synchronous lifespan startup hook."""
    t = time.monotonic()
    audit(f"startup {name}: begin")
    try:
        fn()
        audit(f"startup {name}: done ({time.monotonic() - t:.2f}s)")
    except Exception:
        audit(f"startup {name}: failed ({time.monotonic() - t:.2f}s)")
        raise
