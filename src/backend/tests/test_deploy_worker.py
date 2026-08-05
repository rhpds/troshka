"""Tests for deploy_worker helper functions."""

from unittest.mock import MagicMock, patch


def test_log_job_context_full():
    """Logs structured line with project and host IDs."""
    from app.workers.deploy_worker import _log_job_context

    job = MagicMock()
    job.id = "abcdef12-3456-7890-abcd-ef1234567890"
    job.func_name = "app.workers.jobs.job_deploy_project"
    job.meta = {"project_id": "proj-1234-5678", "host_id": "host-aaaa-bbbb"}

    with patch("app.workers.deploy_worker._logger") as mock_logger:
        _log_job_context(">> START", job, "worker-1")
        mock_logger.info.assert_called_once()
        msg = mock_logger.info.call_args[0][2]
        assert "job=abcdef12" in msg
        assert "func=job_deploy_project" in msg
        assert "project=proj-123" in msg
        assert "host=host-aaa" in msg
        assert "worker=worker-1" in msg


def test_log_job_context_no_meta():
    """Logs without project/host when meta is empty."""
    from app.workers.deploy_worker import _log_job_context

    job = MagicMock()
    job.id = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
    job.func_name = "app.workers.jobs.some_func"
    job.meta = {}

    with patch("app.workers.deploy_worker._logger") as mock_logger:
        _log_job_context("<< DONE", job, "worker-2")
        msg = mock_logger.info.call_args[0][2]
        assert "project=" not in msg
        assert "host=" not in msg
        assert "func=some_func" in msg


def test_log_job_context_no_func_name():
    """Handles None func_name gracefully."""
    from app.workers.deploy_worker import _log_job_context

    job = MagicMock()
    job.id = "11112222-3333-4444-5555-666677778888"
    job.func_name = None
    job.meta = {}

    with patch("app.workers.deploy_worker._logger") as mock_logger:
        _log_job_context(">> START", job, "w")
        msg = mock_logger.info.call_args[0][2]
        assert "func=" in msg


def test_save_worker_meta():
    """Stores pod name on job meta and saves."""
    from app.workers.deploy_worker import _save_worker_meta

    job = MagicMock()
    job.meta = {}

    with patch("app.workers.deploy_worker._POD_NAME", "test-pod-123"):
        _save_worker_meta(job)

    assert job.meta["worker_pod"] == "test-pod-123"
    job.save_meta.assert_called_once()


def test_save_worker_meta_exception():
    """Does not raise when save_meta fails."""
    from app.workers.deploy_worker import _save_worker_meta

    job = MagicMock()
    job.meta = {}
    job.save_meta.side_effect = RuntimeError("Redis down")

    with patch("app.workers.deploy_worker._POD_NAME", "pod-x"):
        _save_worker_meta(job)

    assert job.meta["worker_pod"] == "pod-x"
