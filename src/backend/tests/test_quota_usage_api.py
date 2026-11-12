"""Usage exposes authorized aggregates and exact values, never project rosters."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.models.managed_group import (
    ManagedGroup,
    ManagedGroupManager,
    ManagedGroupMember,
)
from app.models.project import Project, ProjectShare
from app.models.quota import (
    QuotaAction,
    QuotaAllocation,
    QuotaAudit,
    QuotaCharge,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
)
from app.models.user import User
from app.services import quota_accounting
from tests.conftest import TestSession
from tests.test_metering_api import _as_user, client
from tests.test_quota_permissions import quota_case as usage_case_fixture

usage_case = usage_case_fixture


@pytest.fixture
def quota_case(usage_case):
    # Let pytest resume the parent fixture after our cleanup. Closing its yield
    # generator manually skips the authentication override teardown after yield.
    case = usage_case
    try:
        yield case
    finally:
        with TestSession() as db:
            for model in (QuotaAction, QuotaAllocation, QuotaReservation, QuotaCharge):
                db.query(model).filter(model.project_id == case.pid).delete(
                    synchronize_session=False
                )
            for model in (QuotaPolicy, QuotaAudit):
                db.query(model).filter(
                    model.scope_id.in_((case.pid, case.org, case.owner.id))
                ).delete(synchronize_session=False)
            marker = db.get(
                QuotaReadiness,
                str(uuid5(NAMESPACE_URL, f"troshka:enforcement:{case.pid}")),
            )
            if marker:
                db.delete(marker)
            marker = db.get(
                QuotaReadiness,
                str(uuid5(NAMESPACE_URL, f"troshka:reevaluate:user:{case.owner.id}")),
            )
            if marker:
                db.delete(marker)
            project = db.get(Project, case.pid)
            if project:
                db.delete(project)
            groups = (case.org, case.reporting)
            for association in (ManagedGroupManager, ManagedGroupMember):
                db.query(association).filter(association.group_id.in_(groups)).delete(
                    synchronize_session=False
                )
            for user in (
                case.admin,
                case.manager,
                case.owner,
                case.reporter,
                case.other,
            ):
                db.query(User).filter_by(id=user.id).delete(synchronize_session=False)
            db.query(ManagedGroup).filter(ManagedGroup.id.in_(groups)).delete(
                synchronize_session=False
            )
            db.commit()


def usage(scope, identity):
    return f"/api/v1/quotas/{scope}/{identity}/usage"


def test_usage_exact_aggregates_decimal_period_and_no_project_details(
    quota_case, monkeypatch
):
    q = quota_case
    monkeypatch.setattr(
        quota_accounting, "spend", lambda *args: Decimal("123.123456789012")
    )
    with TestSession() as db:
        db.add(
            QuotaAllocation(
                operation_id=f"usage-{q.pid}",
                project_id=q.pid,
                owner_id=q.owner.id,
                organization_id=q.org,
                requested={"ram_bytes": 1099511627777},
                observed={"ram_bytes": 1099511627777},
            )
        )
        db.add(
            QuotaReservation(
                operation_id=f"reserve-{q.pid}",
                project_id=q.pid,
                owner_id=q.owner.id,
                organization_id=q.org,
                requested={"cpu": 4},
                observed={"cpu": 1},
            )
        )
        db.commit()
    _as_user(q.owner)
    response = client.get(usage("organization", q.org))
    assert response.status_code == 200
    body = response.json()
    assert body["used"]["ram_bytes"] == 1099511627777
    assert body["reserved"]["cpu"] == 3
    assert body["spent_usd"] == "123.123456789012"
    assert body["reset_at"].endswith("Z")
    assert body["can_edit"] is False
    assert body["cost_center_id"] is None
    assert q.pid not in response.text
    assert "projects" not in body or isinstance(body["projects"], int)
    project = client.get(usage("project", q.pid)).json()
    assert project["reset_at"] is None
    assert project["can_edit"] is True


def test_unavailable_is_explicit_and_policy_remains_editable_with_feature_off(
    quota_case, monkeypatch
):
    q = quota_case

    def unavailable(*args):
        raise quota_accounting.AccountingUnavailable("not ready")

    monkeypatch.setattr(quota_accounting, "spend", unavailable)
    monkeypatch.setattr(quota_accounting, "quotas_enabled", lambda: False)
    _as_user(q.manager)
    body = client.get(usage("user", q.owner.id)).json()
    assert body["accounting_status"] == "unavailable"
    assert body["spent_usd"] is None
    assert body["as_of"] is None
    assert body["enforcement_enabled"] is False
    assert body["enforcement_status"] == "disabled"
    assert body["can_edit"] is True
    assert (
        client.patch(
            f"/api/v1/quotas/user/{q.owner.id}", json={"spending_usd": "0"}
        ).status_code
        == 200
    )


def test_usage_visibility_and_collaborator_read_only(quota_case):
    q = quota_case
    _as_user(q.reporter)
    assert client.get(usage("user", q.owner.id)).status_code == 403
    assert client.get(usage("organization", q.org)).status_code == 403
    with TestSession() as db:
        db.add(ProjectShare(project_id=q.pid, user_id=q.other.id, permission="edit"))
        db.commit()
    _as_user(q.other)
    response = client.get(usage("project", q.pid))
    assert response.status_code == 200
    assert response.json()["can_edit"] is False
    assert (
        client.patch(f"/api/v1/quotas/project/{q.pid}", json={"cpu": 0}).status_code
        == 403
    )


def test_group_count_uses_authoritative_membership_and_ignores_legacy_rosters(
    quota_case,
):
    q = quota_case
    with TestSession() as db:
        db.add(ManagedGroupMember(group_id=q.org, user_id=q.reporter.id))
        db.commit()
    _as_user(q.admin)
    groups = {g["id"]: g for g in client.get("/api/v1/groups").json()}
    assert groups[q.org]["member_count"] == 1
    assert groups[q.reporting]["member_count"] == 0
    with TestSession() as db:
        # Both stale roster rows remain migration evidence, not current members.
        assert db.get(ManagedGroupMember, (q.org, q.reporter.id)) is not None
        assert db.get(ManagedGroupMember, (q.reporting, q.owner.id)) is not None


def test_actual_monthly_vs_lifetime_charges_and_safe_failed_action_summary(
    quota_case, monkeypatch
):
    from types import SimpleNamespace

    from app.api import quotas

    q = quota_case
    now = datetime(2026, 11, 11, tzinfo=UTC)
    monkeypatch.setattr(quotas, "datetime", SimpleNamespace(now=lambda zone: now))
    monkeypatch.setattr(quota_accounting, "require_ready", lambda db: None)
    with TestSession() as db:
        for month, amount in [(10, "7.25"), (11, "2.50")]:
            db.add(
                QuotaCharge(
                    project_id=q.pid,
                    project_name="Private project name",
                    owner_id=q.owner.id,
                    organization_id=q.org,
                    source="usage-test",
                    source_id=f"{q.pid}-{month}",
                    start_at=datetime(2026, month, 1, tzinfo=UTC),
                    end_at=datetime(2026, month, 2, tzinfo=UTC),
                    kind="vcpu",
                    quantity=1,
                    rate=0,
                    amount=amount,
                )
            )
        db.add(
            QuotaAction(
                scope="user",
                scope_id=q.owner.id,
                policy_revision=0,
                period_key="2026-11",
                kind="stop",
                threshold=100,
                project_id=q.pid,
                state="pending",
            )
        )
        db.add(
            QuotaAction(
                scope="project",
                scope_id=q.pid,
                policy_revision=0,
                period_key="current",
                kind="stop_run",
                threshold=100,
                project_id=q.pid,
                state="failed",
            )
        )
        db.commit()
    _as_user(q.manager)
    body = client.get(usage("user", q.owner.id))
    assert Decimal(body.json()["spent_usd"]) == Decimal("2.5")
    assert body.json()["action_counts"] == {"failed": 1}
    assert "Private project name" not in body.text and q.pid not in body.text
    _as_user(q.owner)
    project = client.get(usage("project", q.pid)).json()
    assert Decimal(project["spent_usd"]) == Decimal("9.75")
    assert project["reset_at"] is None


def test_reauthorizes_after_scope_identity_changes_during_read(quota_case, monkeypatch):
    from app.services import quota_usage

    q = quota_case
    original = quota_usage.authorize_policy
    changed = False

    def race(db, actor, scope, identity):
        nonlocal changed
        target = original(db, actor, scope, identity)
        if not changed:
            changed = True
            with TestSession() as other:
                other.get(User, q.owner.id).organization_id = None
                other.commit()
        return target

    monkeypatch.setattr(quota_usage, "authorize_policy", race)
    monkeypatch.setattr(quota_accounting, "spend", lambda *args: Decimal("1"))
    _as_user(q.manager)
    assert client.get(usage("user", q.owner.id)).status_code == 403


@pytest.mark.parametrize("scope", ["organization", "user"])
@pytest.mark.parametrize(
    "case_name,amount", [("zero", "0"), ("small", "0.000000000001")]
)
def test_persisted_numeric_money_matches_client_contract(
    quota_case, monkeypatch, scope, case_name, amount
):
    from types import SimpleNamespace

    from app.api import quotas

    q = quota_case
    now = datetime(2026, 11, 11, tzinfo=UTC)
    monkeypatch.setattr(quotas, "datetime", SimpleNamespace(now=lambda zone: now))
    monkeypatch.setattr(quota_accounting, "require_ready", lambda db: None)
    identity = q.org if scope == "organization" else q.owner.id
    with TestSession() as db:
        db.add(
            QuotaPolicy(scope=scope, scope_id=identity, spending_usd=Decimal(amount))
        )
        db.add(
            QuotaCharge(
                project_id=q.pid,
                project_name="Numeric contract",
                owner_id=q.owner.id,
                organization_id=q.org,
                source="numeric-contract",
                source_id=q.pid,
                start_at=datetime(2026, 11, 1, tzinfo=UTC),
                end_at=datetime(2026, 11, 2, tzinfo=UTC),
                kind="vcpu",
                quantity=1,
                rate=0,
                amount=Decimal(amount),
            )
        )
        db.commit()
    with TestSession() as db:
        stored = db.query(QuotaPolicy).filter_by(scope=scope, scope_id=identity).one()
        assert stored.spending_usd.as_tuple().exponent == -12
    _as_user(q.admin)
    fixture = (
        Path(__file__).resolve().parents[3]
        / "src/frontend/src/lib/__fixtures__/quota-numeric-money.json"
    )
    expected = json.loads(fixture.read_text())[case_name]
    response = client.get(usage(scope, identity))
    assert response.status_code == 200
    assert response.json()["spent_usd"] == expected["spent_usd"]
    assert (
        response.json()["limits"]["spending_usd"] == expected["limits"]["spending_usd"]
    )
    assert (
        client.get(f"/api/v1/quotas/{scope}/{identity}").json()["spending_usd"]
        == expected["limits"]["spending_usd"]
    )
