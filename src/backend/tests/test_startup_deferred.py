"""Startup must not block on remote-I/O hooks.

A single unreachable provider cluster used to stall the FastAPI lifespan (and
therefore the port bind) for minutes because `_startup_sync_obc_credentials`
made synchronous k8s reads on the startup path. These hooks now run in a
background daemon thread instead.
"""

import threading
import time

import app.main as main


def test_run_deferred_startup_calls_remote_hooks(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.app_updater.resolve_mode", lambda: "image")
    monkeypatch.setattr(
        main, "_startup_resume_storage_pools", lambda: called.append("pools")
    )
    monkeypatch.setattr(
        main, "_startup_sync_obc_credentials", lambda: called.append("obc")
    )
    main._run_deferred_startup()
    assert set(called) == {"pools", "obc"}


def test_run_deferred_startup_isolates_hook_failure(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.app_updater.resolve_mode", lambda: "image")

    def boom():
        raise RuntimeError("cluster unreachable")

    monkeypatch.setattr(main, "_startup_resume_storage_pools", boom)
    monkeypatch.setattr(
        main, "_startup_sync_obc_credentials", lambda: called.append("obc")
    )
    # A failing hook must not propagate, and later hooks must still run.
    main._run_deferred_startup()
    assert called == ["obc"]


def test_start_deferred_startup_is_nonblocking(monkeypatch):
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(2)

    monkeypatch.setattr(main, "_startup_resume_storage_pools", slow)
    monkeypatch.setattr(main, "_startup_sync_obc_credentials", lambda: None)

    t0 = time.time()
    main._start_deferred_startup()
    elapsed = time.time() - t0

    assert elapsed < 0.5, f"startup blocked for {elapsed:.2f}s (should be async)"
    assert started.wait(2.0), "deferred hook never ran in the background thread"
