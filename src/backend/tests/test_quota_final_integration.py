"""Regression contracts for the final application integration boundaries."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from tests.test_quota_entry_points import environment as environment_fixture

environment = environment_fixture


@pytest.mark.parametrize("restart", [False, True])
def test_native_wipe_denied_before_dispatch(environment, monkeypatch, restart):
    from app.api import projects

    db, project, owner, _, host = environment
    host.host_type = "kubevirt-cluster"
    project.host_id = host.id
    project.state = "stopped"
    project.topology = {
        "nodes": [
            {"id": "vm", "type": "vmNode", "data": {}},
            {"id": "disk", "type": "storageNode", "data": {"size": 1}},
        ],
        "edges": [{"source": "disk", "target": "vm", "targetHandle": "dp-disk0"}],
    }
    dispatch = Mock(side_effect=AssertionError("unadmitted initial power-on"))
    monkeypatch.setattr(projects, "_wipe_disk_kubevirt", dispatch)
    with pytest.raises(HTTPException) as error:
        projects.wipe_disk(project.id, "vm", "disk", owner, db, restart=restart)
    assert error.value.status_code == 409
    assert error.value.detail["reason"] == "unsupported_quota_operation"
    dispatch.assert_not_called()


@pytest.mark.parametrize("action", ["delete_project", "undeploy_project"])
@pytest.mark.parametrize("state", ["active", "stopped", "error", "draft"])
def test_native_cleanup_rejected_before_any_side_effect(
    environment, monkeypatch, action, state
):
    from app.api import projects

    db, project, owner, _, host = environment
    host.host_type = "kubevirt-cluster"
    project.host_id, project.state = host.id, state
    db.commit()
    side_effect = Mock(side_effect=AssertionError("cleanup mutated before rejection"))
    monkeypatch.setattr(projects, "_release_project_eips_on_delete", side_effect)
    monkeypatch.setattr(projects, "destroy_project_sync", side_effect)
    monkeypatch.setattr("app.core.redis.enqueue_job", side_effect)
    with pytest.raises(HTTPException) as error:
        getattr(projects, action)(project.id, owner, db)
    assert error.value.status_code == 409
    assert error.value.detail["reason"] == "unsupported_quota_operation"
    assert project.state == state and project.host_id == host.id
    side_effect.assert_not_called()


def test_native_cleanup_worker_rejects_before_fence_or_teardown(
    environment, monkeypatch
):
    from app.services import deploy_service, quota_cleanup

    db, project, _, _, host = environment
    host.host_type = "kubevirt-cluster"
    project.host_id, project.state = host.id, "stopped"
    db.commit()
    mutation = Mock(side_effect=AssertionError("worker side effect before guard"))
    monkeypatch.setattr(quota_cleanup, "fence", mutation)
    monkeypatch.setattr(deploy_service, "_destroy_revoke_ops_pod_key", mutation)
    monkeypatch.setattr(deploy_service, "_destroy_kubevirt_native", mutation)
    assert (
        deploy_service._destroy_project_inner(
            {"project_id": project.id, "host_id": host.id}, delete_record=False
        )
        is False
    )
    mutation.assert_not_called()


@pytest.mark.parametrize("action", ["delete_project", "undeploy_project"])
def test_native_empty_never_deployed_cleanup_remains_supported(
    environment, monkeypatch, action
):
    from app.api import projects
    from app.models.project import Project
    from app.models.provider import Provider

    db, project, owner, _, host = environment
    host.host_type = "kubevirt-cluster"
    db.get(Provider, host.provider_id).type = "kubevirt"
    project.provider_id = host.provider_id
    monkeypatch.setattr(projects, "_release_project_eips_on_delete", lambda *args: None)
    project_id = project.id
    getattr(projects, action)(project.id, owner, db)
    db.expire_all()
    saved = db.get(Project, project_id)
    if action == "delete_project":
        assert saved is None
    else:
        assert saved.state == "draft" and saved.host_id is None


@pytest.mark.parametrize("off_action", ["pause", "hibernate"])
def test_native_unsupported_off_action_rejected_before_state_change(
    environment, monkeypatch, off_action
):
    from app.api import projects

    db, project, owner, _, host = environment
    host.host_type = "kubevirt-cluster"
    project.host_id, project.state, project.off_action = host.id, "active", off_action
    queued = Mock(side_effect=AssertionError("unsupported stop dispatched"))
    monkeypatch.setattr("app.core.redis.enqueue_job", queued)
    with pytest.raises(HTTPException):
        projects.stop_project(project.id, owner, db)
    assert project.state == "active"
    queued.assert_not_called()


def test_native_missing_stop_identity_returns_accounting_denial(environment):
    from app.api import projects

    db, project, owner, _, host = environment
    host.host_type = "kubevirt-cluster"
    project.host_id, project.state = host.id, "active"
    with pytest.raises(HTTPException) as error:
        projects.stop_project(project.id, owner, db)
    assert error.value.status_code == 409
    assert error.value.detail["reason"] == "accounting_unavailable"
    assert project.state == "active"


@pytest.mark.parametrize("timer", ["auto_stop", "auto_delete"])
def test_rejected_native_timer_preserves_project_and_continues_other_timers(
    environment, monkeypatch, timer
):
    from datetime import timedelta

    from app.models.project import Project
    from app.services import project_timer

    db, project, owner, _, host = environment
    host.host_type = "kubevirt-cluster"
    project.host_id, project.state, project.off_action = host.id, "active", "pause"
    now = datetime.now(UTC)
    setattr(
        project,
        "auto_stop_expires_at" if timer == "auto_stop" else "lifetime_expires_at",
        now - timedelta(seconds=1),
    )
    other = Project(
        name="unrelated-timer",
        owner_id=owner.id,
        state="active",
        auto_stop_expires_at=now - timedelta(seconds=1),
    )
    db.add(other)
    db.commit()
    queued = []
    monkeypatch.setattr(project_timer, "_spawn_stop", queued.append)
    monkeypatch.setattr(project_timer, "_notify", lambda *args: None)
    result = {"auto_stop": [], "auto_delete": []}
    try:
        project_timer._process_expired_timers(db, now, result, False)
        assert project.state == "active" and project.host_id == host.id
        assert queued == [other.id]
        assert other.state == "stopping"
    finally:
        db.delete(other)
        db.commit()


@pytest.mark.parametrize("kind", ["invoice", "statement"])
@pytest.mark.parametrize("era", ["historical", "current"])
def test_frozen_billing_documents_preserve_amounts_and_lines(
    environment, monkeypatch, kind, era
):
    from app.api import group_metering, metering
    from app.models.metering import MonthlyStatement, ProjectInvoice

    db, project, owner, org, _ = environment
    owner.role = "admin"
    monkeypatch.setattr(metering, "quotas_enabled", lambda: True)
    readiness = Mock(
        side_effect=AssertionError("frozen document requires no readiness")
    )
    monkeypatch.setattr(metering, "require_ready", readiness)
    start = datetime(2026, 10 if era == "historical" else 11, 1, tzinfo=UTC)
    end = datetime(2026, start.month, 2, tzinfo=UTC)
    lines = {"by_kind": [{"kind": "vcpu", "subtotal": 123.45}]}
    fields = dict(
        owner_id=owner.id,
        total_usd=Decimal("123.450000"),
        line_items=lines,
        period_start=start,
        period_end=end,
        finalized_at=end,
        currency="USD",
    )
    if kind == "invoice":
        row = ProjectInvoice(project_id=project.id, project_name=project.name, **fields)
    else:
        row = MonthlyStatement(status="final", **fields)
    db.add(row)
    db.flush()
    detail = getattr(metering, f"_{kind}_detail")
    owner_view = detail(row, owner.email, db)
    group_view = detail(row, owner.email)
    assert owner_view == group_view
    assert Decimal(str(owner_view["total_usd"])) == Decimal("123.45")
    assert owner_view["line_items"] == lines
    documents = (
        group_metering.invoices if kind == "invoice" else group_metering.statements
    )
    group_rows = documents(org.id, owner, db)
    assert len(group_rows) == 1
    assert group_rows[0]["total_usd"] == owner_view["total_usd"]
    assert group_rows[0]["billing_view"] == "owner_billing_history"
    readiness.assert_not_called()
