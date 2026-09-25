"""Unit tests for workload interrupt / retry helpers."""

import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.workloads.run_service import (
    _INTERRUPT_MSG,
    mark_workload_run_interrupted,
    reconcile_stale_workload_run,
    retry_workload_run,
)


def test_mark_workload_run_interrupted_sets_error():
    run = SimpleNamespace(
        id="run-1",
        status="pending",
        error=None,
        ended_at=None,
    )
    db = MagicMock()
    db.get.return_value = run
    with patch("app.services.workloads.run_service.SessionLocal", return_value=db):
        mark_workload_run_interrupted("run-1", "Interrupted: boom")
    assert run.status == "error"
    assert "Interrupted" in run.error
    assert run.ended_at is not None
    db.commit.assert_called_once()
    db.close.assert_called_once()


def test_mark_workload_run_interrupted_skips_terminal():
    run = SimpleNamespace(id="run-1", status="succeeded", error=None, ended_at=None)
    db = MagicMock()
    db.get.return_value = run
    with patch("app.services.workloads.run_service.SessionLocal", return_value=db):
        mark_workload_run_interrupted("run-1")
    assert run.status == "succeeded"
    db.commit.assert_not_called()


def test_reconcile_stale_pending_flips_to_error():
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    run = SimpleNamespace(
        id="run-stale",
        status="pending",
        started_at=None,
        created_at=old,
        error=None,
        ended_at=None,
    )
    db = MagicMock()
    db.get.return_value = run
    out = reconcile_stale_workload_run(db, run)
    assert out.status == "error"
    assert out.error == _INTERRUPT_MSG
    db.commit.assert_called()


def test_reconcile_fresh_pending_unchanged():
    run = SimpleNamespace(
        id="run-new",
        status="pending",
        started_at=None,
        created_at=datetime.datetime.now(datetime.UTC),
        error=None,
        ended_at=None,
    )
    db = MagicMock()
    out = reconcile_stale_workload_run(db, run)
    assert out.status == "pending"
    db.commit.assert_not_called()


def test_retry_workload_run_clones_and_supersedes_pending():
    pending = SimpleNamespace(
        id="old",
        status="pending",
        started_at=None,
        project_id="p1",
        kind="ad_hoc",
        catalog_item=None,
        role_fqcn="a.b.c",
        target_map={"mode": "cluster"},
        requirements_content=None,
        ee_image=None,
        extra_vars=None,
        owner_id="u1",
        error=None,
        ended_at=None,
    )
    db = MagicMock()
    db.get.return_value = pending
    new_run = SimpleNamespace(id="new", status="pending")
    with patch(
        "app.services.workloads.run_service.start_workload_run", return_value=new_run
    ) as start:
        out = retry_workload_run(db, pending, owner_id="u1")
    assert out is new_run
    assert pending.status == "error"
    assert "Superseded" in pending.error
    start.assert_called_once()
    assert start.call_args.kwargs["role_fqcn"] == "a.b.c"


def test_retry_rejects_running():
    run = SimpleNamespace(status="running", started_at="x")
    try:
        retry_workload_run(MagicMock(), run)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "running" in str(exc)


def test_on_job_failure_marks_workload_run():
    from app.core.redis import _on_job_failure

    job = MagicMock()
    job.id = "job-12345678"
    job.func_name = "app.workers.jobs.job_run_workload"
    job.args = ("run-abc",)
    job.meta = {"project_id": "proj-1"}
    connection = MagicMock()

    with (
        patch("app.core.redis._cleanup_host_locks"),
        patch("app.core.redis.delete_progress"),
        patch("app.core.redis._set_project_error_state"),
        patch(
            "app.services.workloads.run_service.mark_workload_run_interrupted"
        ) as mark,
    ):
        _on_job_failure(job, connection, RuntimeError, RuntimeError("boom"), None)
    mark.assert_called_once()
    assert mark.call_args[0][0] == "run-abc"
    assert "Failed" in mark.call_args[0][1]


def test_on_job_failure_abandoned_marks_interrupted():
    from app.core.redis import _on_job_failure

    class AbandonedJobError(Exception):
        pass

    job = MagicMock()
    job.id = "job-abcd1234"
    job.func_name = "app.workers.jobs.job_run_workload"
    job.args = ("run-xyz",)
    job.meta = {"project_id": "proj-1"}

    with (
        patch("app.core.redis._cleanup_host_locks"),
        patch("app.core.redis.delete_progress"),
        patch("app.core.redis._set_project_error_state"),
        patch(
            "app.services.workloads.run_service.mark_workload_run_interrupted"
        ) as mark,
    ):
        _on_job_failure(
            job, MagicMock(), AbandonedJobError, AbandonedJobError("gone"), None
        )
    mark.assert_called_once()
    assert "Interrupted" in mark.call_args[0][1]
