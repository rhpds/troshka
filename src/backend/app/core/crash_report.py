"""Capture full stack traces on backend process death or fatal errors."""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

RUNTIME_DIR = Path.home() / ".cache" / "troshka"
CRASH_LOG_PATH = RUNTIME_DIR / "backend-crash.log"

_installed = False
_crash_file = None
logger = logging.getLogger(__name__)


def _ensure_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def _write(header: str, frame=None, *, all_threads: bool = True) -> None:
    _ensure_dir()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "",
        f"{'=' * 72}",
        f"{ts} pid={os.getpid()} {header}",
        f"argv={' '.join(sys.argv)}",
    ]
    if frame is not None:
        lines.append("--- handler frame stack ---")
        lines.extend(traceback.format_stack(frame))
    if all_threads:
        lines.append("--- faulthandler all threads ---")
    body = "\n".join(lines) + "\n"
    try:
        with CRASH_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(body)
            if all_threads:
                faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.write(f"{'=' * 72}\n")
            fh.flush()
    except OSError as exc:
        logger.error("crash_report write failed: %s", exc)


def _excepthook(exc_type, exc, tb):
    _write(
        f"UNCAUGHT {exc_type.__name__}: {exc}\n"
        f"{''.join(traceback.format_exception(exc_type, exc, tb))}",
        all_threads=True,
    )
    sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args):
    if args.exc_value is None:
        return
    _write(
        f"THREAD {args.thread.name} {args.exc_type.__name__}: {args.exc_value}\n"
        f"{''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))}",
        all_threads=True,
    )


def _atexit_report():
    _write("atexit — normal process exit", all_threads=False)


def _audit_subprocess_popen(*args, **kwargs):
    _write(
        "subprocess.Popen called — parent stack before fork:\n"
        f"  args={args!r}\n"
        f"  kwargs={ {k: v for k, v in kwargs.items() if k != 'env'} !r}",
        frame=None,
    )
    return _original_popen(*args, **kwargs)


def _audit_subprocess_run(*args, **kwargs):
    _write(
        "subprocess.run called — parent stack before fork:\n"
        f"  args={args!r}\n"
        f"  kwargs={ {k: v for k, v in kwargs.items() if k != 'env'} !r}",
        frame=None,
    )
    return _original_run(*args, **kwargs)


_original_popen = subprocess.Popen
_original_run = subprocess.run


def install_crash_reporting(*, patch_subprocess: bool = True) -> Path:
    """Install crash hooks once. Returns path to crash log file."""
    global _installed, _crash_file
    if _installed:
        return CRASH_LOG_PATH
    _installed = True

    _ensure_dir()
    _crash_file = CRASH_LOG_PATH.open("a", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_crash_file, all_threads=True)
    # SIGABRT/SIGSEGV are already handled by faulthandler.enable(); registering
    # them raises RuntimeError ("signal 6 cannot be registered") on CPython.
    for sig in (signal.SIGUSR1, signal.SIGUSR2):
        try:
            faulthandler.register(sig, file=_crash_file, all_threads=True)
        except (OSError, ValueError, RuntimeError):
            pass

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    atexit.register(_atexit_report)

    if patch_subprocess:
        subprocess.Popen = _audit_subprocess_popen  # type: ignore[assignment]
        subprocess.run = _audit_subprocess_run  # type: ignore[assignment]

    _write("crash_reporting installed", all_threads=False)
    return CRASH_LOG_PATH


def record_signal(signum: int, frame) -> None:
    """Record a signal + stack to the crash log (call from lifecycle wrappers)."""
    try:
        name = signal.Signals(signum).name
    except (ValueError, AttributeError):
        name = str(signum)
    _write(f"SIGNAL {name} ({signum})", frame)


def log_event(message: str) -> None:
    """Write a single-line event to the crash log (e.g. before a risky operation)."""
    _write(message, all_threads=False)
