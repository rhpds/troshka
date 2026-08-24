#!/usr/bin/env python3
"""Stay parent of uvicorn and record the kernel wait status when it exits.

In-process Python hooks never run for SIGKILL (9). A disowned child has
nobody calling waitid(), so the exit reason is discarded. This process is
the parent: it always gets CLD_EXITED / CLD_KILLED / CLD_DUMPED plus the
signal or exit code.
"""

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
EXIT_LOG = RUNTIME_DIR / "backend-exit.log"
CRASH_LOG = RUNTIME_DIR / "backend-crash.log"

_CLD_NAMES = {
    getattr(os, "CLD_EXITED", 1): "CLD_EXITED",  # WIFEXITED — called exit()/return
    getattr(os, "CLD_KILLED", 2): "CLD_KILLED",  # WIFSIGNALED — signal, no core
    getattr(os, "CLD_DUMPED", 3): "CLD_DUMPED",  # WIFSIGNALED — signal + core
    getattr(os, "CLD_TRAPPED", 4): "CLD_TRAPPED",
    getattr(os, "CLD_STOPPED", 5): "CLD_STOPPED",
    getattr(os, "CLD_CONTINUED", 6): "CLD_CONTINUED",
}


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def _signal_name(num: int) -> str:
    try:
        return signal.Signals(num).name
    except (ValueError, AttributeError):
        return f"signal:{num}"


def _format_waitid(child_pid: int, info) -> str:
    code = getattr(info, "si_code", None)
    status = getattr(info, "si_status", None)
    code_name = _CLD_NAMES.get(code, f"si_code={code}")
    extra = ""
    if code in (
        getattr(os, "CLD_KILLED", 2),
        getattr(os, "CLD_DUMPED", 3),
    ):
        extra = (
            f" killed_by={_signal_name(status)} ({status})"
            f"  — in-process atexit/handlers do not run for SIGKILL"
        )
    elif code == getattr(os, "CLD_EXITED", 1):
        extra = f" exit_code={status}"
    return (
        f"{_ts()} supervisor pid={os.getpid()} child={child_pid} "
        f"{code_name} si_status={status} si_uid={getattr(info, 'si_uid', '?')}"
        f"{extra}"
    )


def _format_waitpid(child_pid: int, status: int) -> str:
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        dumped = " core=yes" if os.WCOREDUMP(status) else " core=no"
        return (
            f"{_ts()} supervisor pid={os.getpid()} child={child_pid} "
            f"WIFSIGNALED killed_by={_signal_name(sig)} ({sig}){dumped}"
        )
    if os.WIFEXITED(status):
        return (
            f"{_ts()} supervisor pid={os.getpid()} child={child_pid} "
            f"WIFEXITED exit_code={os.WEXITSTATUS(status)}"
        )
    return (
        f"{_ts()} supervisor pid={os.getpid()} child={child_pid} "
        f"waitpid_status={status}"
    )


def _record(line: str) -> None:
    block = f"\n{'=' * 72}\n" f"{line}\n" f"argv={' '.join(sys.argv)}\n" f"{'=' * 72}\n"
    _append(EXIT_LOG, line)
    _append(LIFECYCLE_LOG, line)
    _append(CRASH_LOG, block)
    print(line, flush=True)


def _wait_child(child_pid: int) -> None:
    if hasattr(os, "waitid") and hasattr(os, "WEXITED"):
        info = os.waitid(os.P_PID, child_pid, os.WEXITED)
        _record(_format_waitid(child_pid, info))
        return
    _pid, status = os.waitpid(child_pid, 0)
    _record(_format_waitpid(child_pid, status))


def _detach_from_caller_session(log_path: Path) -> None:
    """Leave the calling shell's process group.

    Cursor Agent (and similar) SIGKILL the whole PGID when a tool command
    finishes. nohup/disown do not change PGID, so the backend dies with
    no waitid. posix_spawn(setsid=True) starts a new session that survives.
    """
    if os.environ.get("TROSHKA_SUPERVISOR_DETACHED") == "1":
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TROSHKA_SUPERVISOR_DETACHED"] = "1"
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backend-dir", required=True)
    parser.add_argument("--pidfile", required=True)
    parser.add_argument("--supervisor-pidfile", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()

    _detach_from_caller_session(Path(args.log))

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    Path(args.supervisor_pidfile).write_text(f"{os.getpid()}\n")

    env = os.environ.copy()
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    uvicorn = str(Path(args.backend_dir) / "venv" / "bin" / "uvicorn")
    argv = [
        uvicorn,
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(args.port),
    ]

    # No threads in this process yet — fork/exec is safe here.
    child = subprocess.Popen(argv, cwd=args.backend_dir, env=env)
    Path(args.pidfile).write_text(f"{child.pid}\n")
    _append(
        LIFECYCLE_LOG,
        f"{_ts()} supervisor pid={os.getpid()} pgid={os.getpgrp()} "
        f"sid={os.getsid(0)} ppid={os.getppid()} spawned uvicorn pid={child.pid} "
        f"port={args.port}",
    )

    def _forward(signum, _frame):
        try:
            os.kill(child.pid, signum)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGHUP, _forward)

    try:
        _wait_child(child.pid)
    except Exception as exc:
        _record(
            f"{_ts()} supervisor pid={os.getpid()} child={child.pid} "
            f"wait_failed {type(exc).__name__}: {exc}"
        )
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
