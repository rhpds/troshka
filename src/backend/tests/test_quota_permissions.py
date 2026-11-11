"""Quota delegation follows authoritative organization ownership, not reporting."""

import uuid
from types import SimpleNamespace

import pytest

from app.core.auth import get_current_user
from app.main import app
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaAudit, QuotaPolicy, QuotaReadiness
from app.models.user import User
from tests.conftest import TestSession
from tests.test_groups_api import make_group
from tests.test_metering_api import _add_user, _as_user, client


@pytest.fixture
def quota_case():
    users = [
        _add_user(f"{name}-{uuid.uuid4().hex}@test.invalid", role)
        for name, role in [
            ("admin", "admin"),
            ("manager", "user"),
            ("owner", "user"),
            ("reporter", "user"),
            ("other", "user"),
        ]
    ]
    admin, manager, owner, reporter, other = users
    org = make_group(admin, manager, other)
    reporting = make_group(admin, reporter, owner)
    with TestSession() as db:
        from app.models.managed_group import ManagedGroup

        db.get(ManagedGroup, org).is_organization = True
        db.get(User, owner.id).organization_id = org
        project = Project(
            name="Quota project", owner_id=owner.id, state="draft", budget_usd=12
        )
        db.add(project)
        db.commit()
        pid = project.id
    yield SimpleNamespace(
        admin=admin,
        manager=manager,
        owner=owner,
        reporter=reporter,
        other=other,
        org=org,
        reporting=reporting,
        pid=pid,
    )
    app.dependency_overrides.pop(get_current_user, None)


def policy(scope, identity):
    return f"/api/v1/quotas/{scope}/{identity}"


def test_organization_admin_can_edit_only_authoritative_users(quota_case):
    q = quota_case
    _as_user(q.manager)
    assert client.patch(policy("user", q.owner.id), json={"cpu": 4}).status_code == 200
    assert client.patch(policy("user", q.other.id), json={"cpu": 4}).status_code in (
        403,
        404,
    )
    assert (
        client.patch(policy("organization", q.org), json={"cpu": 40}).status_code == 403
    )
    assert client.get(policy("organization", q.org)).status_code == 200


def test_reporting_membership_does_not_grant_quota_authority(quota_case):
    q = quota_case
    _as_user(q.reporter)
    assert client.patch(policy("user", q.owner.id), json={"cpu": 4}).status_code in (
        403,
        404,
    )
    assert client.get(policy("user", q.owner.id)).status_code in (403, 404)


def test_cross_organization_admin_cannot_change_user_policy(quota_case):
    q = quota_case
    from app.models.managed_group import ManagedGroup

    other_org = make_group(q.admin, q.reporter, q.owner)
    with TestSession() as db:
        db.get(ManagedGroup, other_org).is_organization = True
        db.commit()
    _as_user(q.reporter)
    assert client.patch(policy("user", q.owner.id), json={"cpu": 1}).status_code == 403


def test_policy_service_changes_rollback_with_audit(quota_case):
    from datetime import UTC, datetime

    from app.schemas.quota import Limits
    from app.services.quota_policy import set_limits

    q = quota_case
    with TestSession() as db:
        set_limits(
            db,
            db.get(User, q.admin.id),
            "user",
            q.owner.id,
            Limits(cpu=2, spending_usd="3"),
            datetime.now(UTC),
        )
        db.rollback()
    with TestSession() as db:
        assert (
            db.query(QuotaPolicy).filter_by(scope="user", scope_id=q.owner.id).count()
            == 0
        )
        assert (
            db.query(QuotaAudit).filter_by(scope="user", scope_id=q.owner.id).count()
            == 0
        )


def test_owner_self_read_and_project_control(quota_case):
    q = quota_case
    _as_user(q.owner)
    assert client.get(policy("user", q.owner.id)).status_code == 200
    assert client.patch(policy("user", q.owner.id), json={"cpu": 4}).status_code == 403
    assert client.get(policy("project", q.pid)).json()["spending_usd"] == "12.000000"
    assert (
        client.patch(
            policy("project", q.pid), json={"cpu": 0, "spending_usd": "8.25"}
        ).status_code
        == 200
    )
    with TestSession() as db:
        assert str(db.get(Project, q.pid).budget_usd) == "8.250000"


def test_unrelated_user_cannot_read_or_edit_project_limits(quota_case):
    q = quota_case
    _as_user(q.other)
    assert client.get(policy("project", q.pid)).status_code in (403, 404)
    assert client.patch(policy("project", q.pid), json={"cpu": 4}).status_code in (
        403,
        404,
    )
    assert (
        client.patch(f"/api/v1/projects/{q.pid}", json={"budget_usd": 999}).status_code
        == 403
    )


def test_patch_merge_null_audit_and_durable_spending_request(quota_case):
    q = quota_case
    _as_user(q.admin)
    path = policy("user", q.owner.id)
    assert client.patch(path, json={"cpu": 4, "ram_bytes": 1024}).status_code == 200
    response = client.patch(path, json={"cpu": None, "spending_usd": "0"})
    assert response.status_code == 200
    assert response.json()["cpu"] is None
    assert response.json()["ram_bytes"] == 1024
    with TestSession() as db:
        stored = (
            db.query(QuotaPolicy).filter_by(scope="user", scope_id=q.owner.id).one()
        )
        assert stored.revision == 2
        audit = (
            db.query(QuotaAudit)
            .filter_by(scope="user", scope_id=q.owner.id)
            .order_by(QuotaAudit.created_at)
            .all()
        )
        assert len(audit) == 2
        assert audit[-1].actor_id == q.admin.id
        request = next(
            row
            for row in db.query(QuotaReadiness).all()
            if row.details.get("scope_id") == q.owner.id
        )
        assert request.details["policy_revision"] == 2


def test_org_manager_cannot_delegate_own_authority(quota_case):
    q = quota_case
    _as_user(q.manager)
    assert (
        client.post(
            f"/api/v1/groups/{q.org}/managers", json={"email": q.other.email}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/groups/{q.org}/managers/{q.manager.id}").status_code
        == 403
    )
    _as_user(q.admin)
    assert (
        client.post(
            f"/api/v1/groups/{q.org}/managers", json={"email": q.other.email}
        ).status_code
        == 200
    )
    with TestSession() as db:
        assert (
            db.query(QuotaAudit)
            .filter_by(scope_id=q.org, action="organization_managers")
            .count()
            == 1
        )


def test_deleting_an_organization_administrator_audits_revocation(quota_case):
    q = quota_case
    _as_user(q.admin)
    client.post(f"/api/v1/groups/{q.org}/managers", json={"email": q.other.email})
    assert client.delete(f"/api/v1/users/{q.manager.id}").status_code == 204
    with TestSession() as db:
        audit = (
            db.query(QuotaAudit)
            .filter_by(scope_id=q.org, action="organization_managers")
            .all()
        )
        assert len(audit) == 2
        removed = next(
            row for row in audit if q.manager.id not in row.after["manager_ids"]
        )
        assert q.manager.id in removed.before["manager_ids"]
        assert removed.actor_id == q.admin.id


def test_organization_discovery_and_members_use_authoritative_association(quota_case):
    q = quota_case
    _as_user(q.manager)
    response = client.get("/api/v1/quotas/organizations")
    assert response.status_code == 200
    assert {r["id"] for r in response.json()} == {q.org}
    members = client.get(f"/api/v1/quotas/organizations/{q.org}/members")
    assert members.status_code == 200
    assert [r["id"] for r in members.json()] == [q.owner.id]
    _as_user(q.reporter)
    assert client.get(f"/api/v1/quotas/organizations/{q.org}/members").status_code in (
        403,
        404,
    )


def test_platform_admin_org_designation_cost_center_and_deletion_guard(quota_case):
    q = quota_case
    _as_user(q.admin)
    response = client.post(
        "/api/v1/groups",
        json={
            "name": uuid.uuid4().hex,
            "is_organization": True,
            "cost_center_id": "CC42",
        },
    )
    assert response.status_code == 201
    assert response.json()["is_organization"] is True
    assert response.json()["cost_center_id"] == "CC42"
    organization_id = response.json()["id"]
    updated = client.patch(
        f"/api/v1/groups/{organization_id}", json={"cost_center_id": None}
    )
    assert updated.status_code == 200
    assert updated.json()["cost_center_id"] is None
    assert client.delete(f"/api/v1/groups/{organization_id}").status_code == 204
    assert (
        client.patch(
            f"/api/v1/groups/{q.org}", json={"is_organization": False}
        ).status_code
        == 409
    )
    assert client.delete(f"/api/v1/groups/{q.org}").status_code == 409


def test_organization_with_ledger_allocation_cannot_be_deleted(quota_case):
    q = quota_case
    from app.models.managed_group import ManagedGroup

    org = make_group(q.admin, q.manager, q.other)
    with TestSession() as db:
        db.get(ManagedGroup, org).is_organization = True
        db.add(
            QuotaAllocation(
                operation_id=uuid.uuid4().hex,
                project_id=q.pid,
                owner_id=q.owner.id,
                organization_id=org,
                requested={"cpu": 1},
                observed={"cpu": 1},
            )
        )
        db.commit()
    assert client.delete(f"/api/v1/groups/{org}").status_code == 409
    assert (
        client.patch(
            f"/api/v1/groups/{org}", json={"is_organization": False}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("value", [{"cpu": 2}, None])
def test_legacy_quota_override_writes_are_explicitly_rejected(quota_case, value):
    q = quota_case
    _as_user(q.admin)
    assert (
        client.patch(
            f"/api/v1/users/{q.owner.id}", json={"quota_overrides": value}
        ).status_code
        == 422
    )


def test_legacy_budget_patch_uses_audited_policy_path(quota_case):
    q = quota_case
    _as_user(q.owner)
    assert (
        client.patch(f"/api/v1/projects/{q.pid}", json={"budget_usd": 3}).status_code
        == 200
    )
    assert client.get(policy("project", q.pid)).json()["spending_usd"] == "3.000000"
    with TestSession() as db:
        assert (
            db.query(QuotaAudit).filter_by(scope="project", scope_id=q.pid).count() == 1
        )


@pytest.mark.parametrize("amount", ["1.0000001", "1000000000000"])
def test_project_policy_rejects_unrepresentable_legacy_budget(quota_case, amount):
    q = quota_case
    _as_user(q.owner)
    assert (
        client.patch(
            policy("project", q.pid), json={"spending_usd": amount}
        ).status_code
        == 422
    )
    with TestSession() as db:
        assert db.get(Project, q.pid).budget_usd == 12
        assert (
            db.query(QuotaAudit).filter_by(scope="project", scope_id=q.pid).count() == 0
        )


def test_legacy_budget_rejects_negative_and_does_not_modify_other_fields(quota_case):
    q = quota_case
    _as_user(q.owner)
    response = client.patch(
        f"/api/v1/projects/{q.pid}", json={"budget_usd": -1, "name": "Changed"}
    )
    assert response.status_code == 422
    with TestSession() as db:
        assert db.get(Project, q.pid).name == "Quota project"


def test_clearing_project_spending_restores_unlimited_without_restarting(quota_case):
    q = quota_case
    with TestSession() as db:
        project = db.get(Project, q.pid)
        project.budget_warned = True
        project.budget_stopped = True
        db.commit()
    _as_user(q.owner)
    assert (
        client.patch(policy("project", q.pid), json={"spending_usd": None}).status_code
        == 200
    )
    with TestSession() as db:
        project = db.get(Project, q.pid)
        assert project.budget_usd is None
        assert project.budget_warned is False
        assert project.budget_stopped is False
        assert project.state == "draft"


@pytest.mark.parametrize(
    "body", [{"cpu": -1}, {"cpu": 1.5}, {"spending_usd": "NaN"}, {"unknown": 3}]
)
def test_invalid_limits_rejected(quota_case, body):
    _as_user(quota_case.admin)
    assert (
        client.patch(policy("user", quota_case.owner.id), json=body).status_code == 422
    )
