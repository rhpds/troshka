"""Startup must recognize durable native reconciliation without reissuing work."""

from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from app.models.project import Project
from app.models.quota import QuotaReservation
from tests.test_native_managed_deploy import deploy_case as deploy_case_fixture
from tests.test_native_managed_deploy import initialized, start
from tests.test_native_managed_resume import (
    isolated_quota_database as isolated_database_fixture,
)
from tests.test_native_managed_resume import resume_case as resume_case_fixture
from tests.test_quota_native_storage import native_ledger as native_ledger_fixture

isolated_quota_database = isolated_database_fixture
native_ledger = native_ledger_fixture
resume_case = resume_case_fixture
deploy_case = deploy_case_fixture


def startup(monkeypatch, job_status):
    with (
        patch("app.core.crash_report.install_crash_reporting"),
        patch("app.core.database.init_db"),
    ):
        from app.main import _startup_reset_stuck_projects

    queued, cleared = Mock(), Mock()
    monkeypatch.setattr(
        "app.core.redis.is_redis_available", lambda: job_status is not None
    )
    monkeypatch.setattr(
        "app.core.redis.get_job_info",
        lambda project_id: {"status": job_status} if job_status else None,
    )
    monkeypatch.setattr("app.core.redis.enqueue_job", queued)
    monkeypatch.setattr("app.services.deploy_service._delete_deploy_progress", cleared)
    _startup_reset_stuck_projects()
    queued.assert_not_called()
    return cleared


@pytest.mark.parametrize("phase", ["bound", "initialization-intent"])
@pytest.mark.parametrize("job_status", [None, "finished"])
def test_startup_preserves_valid_native_handoff(
    deploy_case, monkeypatch, phase, job_status
):
    q = deploy_case
    operation_id = initialized(q) if phase == "initialization-intent" else start(q)
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        assert project.state == "deploying" and not project.deploy_step
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.dispatch_metadata["native_deploy"]["phase"] == phase
        before = deepcopy(row.dispatch_metadata)
    calls = deepcopy(q.apis[q.new_id].calls)
    cleared = startup(monkeypatch, job_status)
    cleared.assert_not_called()
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        assert project.state == "deploying" and not project.deploy_error
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.dispatch_metadata == before and row.state == "reconciling"
    assert q.apis[q.new_id].calls == calls


@pytest.mark.parametrize(
    "invalid",
    [
        "cancelled",
        "unfinished",
        "reserved",
        "wrong-host",
        "ordinary",
        "failed-job",
    ],
)
def test_startup_resets_invalid_native_handoff(deploy_case, monkeypatch, invalid):
    q = deploy_case
    operation_id = start(q)
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        metadata = deepcopy(row.dispatch_metadata)
        if invalid == "cancelled":
            metadata["cancelled"] = True
        elif invalid == "unfinished":
            metadata["worker_finished"] = False
        elif invalid == "reserved":
            metadata["native_deploy"]["phase"] = "reserved"
        elif invalid == "wrong-host":
            project.host_id = None
        elif invalid == "ordinary":
            metadata.pop("native_deploy")
        row.dispatch_metadata = metadata
        db.commit()
    calls = deepcopy(q.apis[q.new_id].calls)
    cleared = startup(monkeypatch, "failed" if invalid == "failed-job" else "finished")
    cleared.assert_called_once_with(q.new_id)
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        assert project.state == "error"
        assert project.deploy_error == "Server restarted while project was deploying"
    assert q.apis[q.new_id].calls == calls
