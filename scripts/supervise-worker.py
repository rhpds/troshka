#!/usr/bin/env python3
"""Supervise deploy_worker: restart on exit so dev jobs are not left queued."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

RUNTIME_DIR = Path.home() / ".cache" / "troshka"
LIFECYCLE_LOG = RUNTIME_DIR / "lifecycle.log"


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def _detach_from_caller_session(log_path: Path) -> None:
    """Leave the calling shell's process group (survives Cursor Agent PGID SIGKILL)."""
    if os.environ.get("TROSHKA_WORKER_SUPERVISOR_DETACHED") == "1":
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TROSHKA_WORKER_SUPERVISOR_DETACHED"] = "1"
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        file_actions = [
            (os.POSIX_SPAWN_DUP2, devnull, 0),
            (os.POSIX_SPAWN_DUP2, log_fd, 1),
            (os.POSIX_SPAWN_DUP2, log_fd, 2),
            (os.POSIX_SPAWN_CLOSE, log_fd),
            (os.POSIX_SPAWN_CLOSE, devnull),
        ]
        os.posix_spawn(
            sys.executable,
            [sys.executable, *sys.argv],
            env,
            file_actions=file_actions,
            setsid=True,
        )
    finally:
        os.close(log_fd)
        os.close(devnull)
    raise SystemExit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise troshka deploy_worker")
    parser.add_argument("--backend-dir", required=True)
    parser.add_argument("--worker-pidfile", required=True)
    parser.add_argument("--supervisor-pidfile", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()

    backend = Path(args.backend_dir)
    python = backend / "venv" / "bin" / "python3"
    if not python.is_file():
        print(f"worker supervisor: missing venv python at {python}", file=sys.stderr)
        return 1

    worker_pidfile = Path(args.worker_pidfile)
    supervisor_pidfile = Path(args.supervisor_pidfile)
    log_path = Path(args.log)

    _detach_from_caller_session(log_path)

    supervisor_pidfile.write_text(str(os.getpid()), encoding="utf-8")
    _append(LIFECYCLE_LOG, f"{_ts()} supervise-worker begin supervisor={os.getpid()}")

    child: subprocess.Popen | None = None
    shutting_down = False

    def _stop_child(signum: int, _frame) -> None:
        nonlocal shutting_down
        shutting_down = True
        _append(LIFECYCLE_LOG, f"{_ts()} supervise-worker signal {_stop_name(signum)}")
        if child and child.poll() is None:
            child.terminate()

    def _stop_name(signum: int) -> str:
        try:
            return signal.Signals(signum).name
        except (ValueError, AttributeError):
            return str(signum)

    signal.signal(signal.SIGTERM, _stop_child)
    signal.signal(signal.SIGINT, _stop_child)

    env = os.environ.copy()
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

    while not shutting_down:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_fh:
            child = subprocess.Popen(
                [str(python), "-m", "app.workers.deploy_worker"],
                cwd=str(backend),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
            )
        worker_pidfile.write_text(str(child.pid), encoding="utf-8")
        _append(LIFECYCLE_LOG, f"{_ts()} supervise-worker spawned worker pid={child.pid}")

        code = child.wait()
        child = None
        worker_pidfile.unlink(missing_ok=True)

        if shutting_down:
            break

        _append(
            LIFECYCLE_LOG,
            f"{_ts()} supervise-worker worker exited code={code}, restarting in 2s",
        )
        time.sleep(2)

    worker_pidfile.unlink(missing_ok=True)
    supervisor_pidfile.unlink(missing_ok=True)
    _append(LIFECYCLE_LOG, f"{_ts()} supervise-worker done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
