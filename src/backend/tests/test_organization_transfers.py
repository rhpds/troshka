"""Atomic membership moves preserve durable charges and dispatch identities."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.managed_group import (
    ManagedGroup,
    ManagedGroupManager,
    ManagedGroupMember,
)
from app.models.metering import MeteringInterval
from app.models.project import Project
from app.models.quota import (
    QuotaAllocation,
    QuotaAudit,
    QuotaCharge,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
)
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, QuotaDenied, Resources
from app.services import quota_accounting as accounting
from app.services.group_access import billing_owner_ids
from app.services.group_metering import group_report, scoped_members
from app.services.organization_service import transfer_user
from tests.conftest import TestSession
from tests.test_quota_concurrency import postgres as postgres_fixture
from tests.test_quota_permissions import quota_case as quota_case_fixture

postgres = postgres_fixture
quota_case = quota_case_fixture

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


@pytest.fixture
def api_quota_state():
    models = (
        QuotaReadiness,
        QuotaPolicy,
        QuotaAllocation,
        QuotaReservation,
        QuotaCharge,
        QuotaAudit,
    )
    with TestSession() as db:
        identities = {
            model: [identity for (identity,) in db.query(model.id)] for model in models
        }
        existing = db.get(QuotaReadiness, "baseline")
        before = (
            {
                "ready": existing.ready,
                "details": existing.details,
                "checked_at": existing.checked_at,
                "baseline_version": existing.baseline_version,
            }
            if existing
            else None
        )
    yield
    with TestSession.begin() as db:
        for model in models:
            db.query(model).filter(model.id.not_in(identities[model])).delete(
                synchronize_session=False
            )
        if before is not None:
            db.merge(QuotaReadiness(id="baseline", **before))


@pytest.fixture
def transfer_case(monkeypatch, isolated_quota_database):
    db = isolated_quota_database()
    orgs = [
        ManagedGroup(
            name=uuid4().hex, normalized_name=uuid4().hex, is_organization=True
        )
        for _ in range(2)
    ]
    db.add_all(orgs)
    db.flush()
    admin = User(email=f"{uuid4()}@test.invalid", role="admin")
    owner = User(email=f"{uuid4()}@test.invalid", organization_id=orgs[0].id)
    manager = User(email=f"{uuid4()}@test.invalid")
    db.add_all([admin, owner, manager])
    db.flush()
    project = Project(name="transfer-me", owner_id=owner.id, state="running")
    db.add(project)
    db.merge(
        QuotaReadiness(
            id="baseline",
            ready=True,
            baseline_version="operator-baseline-v2-allocated",
            details={"accounting_ready": True},
        )
    )
    db.flush()
    interval = MeteringInterval(
        project_id=project.id,
        kind="vcpu",
        resource_id="vm",
        qty=2,
        unit_rate=3,
        started_at=NOW - timedelta(hours=1),
        quota_attribution=accounting.attribution(db, project),
    )
    metadata = {
        "generation_id": "generation",
        "claim": "claim",
        "calls": {"ticket": {"job_id": "job"}},
    }
    reservation = QuotaReservation(
        operation_id=uuid4().hex,
        project_id=project.id,
        owner_id=owner.id,
        organization_id=orgs[0].id,
        requested=Resources(cpu=6, storage_bytes=20, projects=1).model_dump(),
        observed=Resources(cpu=2, storage_bytes=10, projects=1).model_dump(),
        dispatch_metadata=metadata,
        state="reconciling",
    )
    db.add_all([interval, reservation])
    db.flush()
    allocation = QuotaAllocation(
        operation_id=reservation.operation_id,
        project_id=project.id,
        owner_id=owner.id,
        organization_id=orgs[0].id,
        requested=reservation.requested,
        observed=reservation.observed,
        observation_id="observed",
        observed_at=NOW - timedelta(minutes=1),
    )
    db.add(allocation)
    db.flush()
    # Provider observations are orthogonal to transfer; retain the known source.
    monkeypatch.setattr(
        "app.services.metering_service.reconcile_project", lambda *a, **kw: None
    )
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    yield SimpleNamespace(
        db=db,
        source=orgs[0],
        destination=orgs[1],
        admin=admin,
        owner=owner,
        manager=manager,
        project=project,
        interval=interval,
        reservation=reservation,
        allocation=allocation,
        metadata=metadata,
    )
    db.rollback()
    db.close()


@pytest.fixture
def enabled_transfer(transfer_case, monkeypatch):
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    return transfer_case


def move(q, destination=True):
    return transfer_user(
        q.db, q.admin, q.owner.id, q.destination.id if destination else None, NOW
    )


def test_transfer_moves_capacity_and_future_costs_without_resetting_any_history(
    transfer_case,
):
    q = transfer_case
    q.db.add(QuotaPolicy(scope="user", scope_id=q.owner.id, cpu=8, spending_usd=50))
    q.project.budget_usd = 30
    result = move(q)
    assert result["organization_id"] == q.destination.id
    assert q.owner.organization_id == q.destination.id
    assert (
        q.reservation.organization_id
        == q.allocation.organization_id
        == q.destination.id
    )
    assert q.reservation.dispatch_metadata == q.metadata
    assert q.allocation.observation_id == "observed"
    accounting.accrue(q.db, q.project.id, NOW + timedelta(hours=1))
    charges = q.db.query(QuotaCharge).filter_by(project_id=q.project.id).all()
    assert {r.organization_id: r.amount for r in charges} == {
        q.source.id: Decimal(6),
        q.destination.id: Decimal(6),
    }
    assert sum(r.amount for r in accounting.charge_rows(q.db, "user", q.owner.id)) == 12
    assert (
        sum(r.amount for r in accounting.charge_rows(q.db, "project", q.project.id))
        == 12
    )
    assert (
        q.db.query(QuotaPolicy).filter_by(scope="user", scope_id=q.owner.id).one().cpu
        == 8
    )
    assert q.project.budget_usd == 30
    audit = (
        q.db.query(QuotaAudit)
        .filter_by(scope_id=q.owner.id, action="organization_transfer")
        .one()
    )
    assert audit.before["organization_id"] == q.source.id
    assert audit.after["organization_id"] == q.destination.id


def test_disabled_live_transfer_preserves_evidence_without_readiness_or_policy_actions(
    transfer_case, monkeypatch
):
    q = transfer_case
    baseline = q.db.get(QuotaReadiness, "baseline")
    baseline.ready = False
    baseline.details = {"accounting_ready": False, "native_capacity": "unknown"}
    q.db.add_all(
        [
            QuotaPolicy(
                scope="organization", scope_id=q.destination.id, cpu=0, spending_usd=0
            ),
            QuotaPolicy(scope="user", scope_id=q.owner.id, cpu=8, spending_usd=50),
        ]
    )
    q.project.budget_usd = 30
    q.project.topology = {"showroom": {"image": "unsupported"}}

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Disabled transfer attempted provider/readiness/spending validation"
        )

    monkeypatch.setattr(accounting, "require_ready", forbidden)
    monkeypatch.setattr(accounting, "spend", forbidden)
    monkeypatch.setattr("app.services.metering_service.reconcile_project", forbidden)
    result = move(q)
    assert result["enforcement_state"] == "inactive"
    assert result["capacity_state"] == result["accounting_state"] == "unverified"
    assert result["spending_state"] == "inactive"
    assert q.owner.organization_id == q.destination.id
    assert baseline.ready is False
    assert baseline.details == {"accounting_ready": False, "native_capacity": "unknown"}
    assert q.db.query(QuotaReadiness).count() == 1
    assert q.db.query(QuotaAllocation).count() == 1
    assert (
        q.allocation.organization_id
        == q.reservation.organization_id
        == q.destination.id
    )
    assert q.reservation.state == "reconciling"
    assert q.reservation.dispatch_metadata == q.metadata
    source, successor = (
        q.db.query(MeteringInterval).order_by(MeteringInterval.started_at).all()
    )
    assert (
        accounting.utc(source.ended_at) == accounting.utc(successor.started_at) == NOW
    )
    assert source.quota_attribution["organization_id"] == q.source.id
    assert successor.quota_attribution["organization_id"] == q.destination.id
    assert source.qty == successor.qty == 2
    assert source.unit_rate == successor.unit_rate == 3
    assert q.db.query(QuotaCharge).one().organization_id == q.source.id
    assert (
        q.db.query(QuotaPolicy).filter_by(scope="user", scope_id=q.owner.id).one().cpu
        == 8
    )
    assert q.project.budget_usd == 30


def test_disabled_transfer_retains_unknown_closed_history_without_guessing(
    transfer_case,
):
    q = transfer_case
    historic = MeteringInterval(
        project_id=q.project.id,
        kind="vcpu",
        resource_id="old",
        qty=1,
        unit_rate=7,
        started_at=NOW - timedelta(hours=3),
        ended_at=NOW - timedelta(hours=2),
    )
    q.db.add(historic)
    move(q)
    assert historic.quota_attribution is None
    assert q.db.query(QuotaCharge).filter_by(source_id=historic.id).count() == 0
    assert (
        q.db.query(QuotaCharge).filter_by(organization_id=q.destination.id).count() == 0
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown",
        "malformed",
        "wrong_owner",
        "wrong_org",
        "early_cursor",
        "duplicate",
        "overlap",
    ],
)
def test_disabled_transfer_rejects_ambiguous_live_sources(transfer_case, invalid):
    q = transfer_case
    if invalid == "unknown":
        q.interval.quota_attribution = None
    elif invalid == "malformed":
        q.interval.quota_attribution = {
            "project_id": q.project.id,
            "owner_id": q.owner.id,
            "organization_id": q.source.id,
        }
    elif invalid == "early_cursor":
        q.interval.quota_accrued_through = NOW - timedelta(hours=2)
    elif invalid in {"duplicate", "overlap"}:
        q.db.add(
            MeteringInterval(
                project_id=q.project.id,
                kind=q.interval.kind,
                resource_id=q.interval.resource_id,
                qty=2,
                unit_rate=3,
                started_at=q.interval.started_at,
                ended_at=NOW - timedelta(minutes=30) if invalid == "overlap" else None,
                quota_attribution=q.interval.quota_attribution,
            )
        )
    else:
        key = "owner_id" if invalid == "wrong_owner" else "organization_id"
        q.interval.quota_attribution = {
            **q.interval.quota_attribution,
            key: str(uuid4()),
        }
    with q.db.begin_nested() as savepoint:
        with pytest.raises(AccountingUnavailable):
            move(q)
        savepoint.rollback()
    assert q.owner.organization_id == q.source.id
    assert q.interval.ended_at is None


def test_disabled_transfer_rejects_inconsistent_confirmed_reservation(transfer_case):
    q = transfer_case
    q.reservation.state = "confirmed"
    q.reservation.organization_id = q.destination.id
    with pytest.raises(AccountingUnavailable, match="identity"):
        move(q)
    assert q.allocation.organization_id == q.source.id


@pytest.mark.parametrize("ledger_kind", ["allocation", "reservation"])
@pytest.mark.parametrize("draft_removal", [False, True])
def test_wrong_owner_project_ledger_blocks_transfer_and_draft_unassignment(
    transfer_case, ledger_kind, draft_removal
):
    from sqlalchemy.orm import Session

    q = transfer_case
    row = q.allocation if ledger_kind == "allocation" else q.reservation
    other = q.reservation if ledger_kind == "allocation" else q.allocation
    q.db.delete(other)
    row.owner_id = q.manager.id
    if draft_removal:
        q.db.delete(q.interval)
        q.project.state = "draft"
    ids = (q.admin.id, q.owner.id, q.source.id, q.destination.id, q.project.id)
    row_id, operation_id = row.id, row.operation_id
    wrong_owner = q.manager.id
    requested, observed = row.requested, row.observed
    state, updated_at, observation_id = row.state, row.updated_at, row.observation_id
    q.db.commit()
    model = QuotaAllocation if ledger_kind == "allocation" else QuotaReservation

    with pytest.raises(AccountingUnavailable, match="identity"):
        with Session(q.db.get_bind()) as db, db.begin():
            transfer_user(
                db, db.get(User, ids[0]), ids[1], None if draft_removal else ids[3], NOW
            )

    with Session(q.db.get_bind()) as db:
        assert db.get(User, ids[1]).organization_id == ids[2]
        assert db.get(Project, ids[4]).state == (
            "draft" if draft_removal else "running"
        )
        retained = db.get(model, row_id)
        assert retained.owner_id == wrong_owner
        assert retained.project_id == ids[4]
        assert retained.organization_id == ids[2]
        assert retained.operation_id == operation_id
        assert retained.requested == requested
        assert retained.observed == observed
        assert (retained.state, retained.updated_at, retained.observation_id) == (
            state,
            updated_at,
            observation_id,
        )
        if ledger_kind == "reservation":
            assert retained.dispatch_metadata == q.metadata
        assert db.query(QuotaAllocation).count() == (ledger_kind == "allocation")
        assert db.query(QuotaReservation).count() == (ledger_kind == "reservation")
        intervals = db.query(MeteringInterval).filter_by(project_id=ids[4]).all()
        assert len(intervals) == (0 if draft_removal else 1)
        if intervals:
            assert intervals[0].ended_at is None
            assert intervals[0].quota_attribution["organization_id"] == ids[2]
            assert intervals[0].qty == 2
            assert intervals[0].unit_rate == 3
        assert db.query(QuotaCharge).count() == db.query(QuotaAudit).count() == 0


def test_multiple_transfers_preserve_closed_stamps_from_previous_groups(transfer_case):
    q = transfer_case
    move(q)
    original_id = q.source.id
    q.db.delete(q.source)  # Retained historical attribution needs no live group row.
    third = ManagedGroup(name=uuid4().hex, normalized_name=uuid4().hex)
    q.db.add(third)
    q.db.flush()
    transfer_user(q.db, q.admin, q.owner.id, third.id, NOW + timedelta(hours=1))
    assert {r.organization_id: r.amount for r in q.db.query(QuotaCharge)} == {
        original_id: Decimal(6),
        q.destination.id: Decimal(6),
    }
    assert (
        q.db.query(MeteringInterval)
        .filter_by(ended_at=None)
        .one()
        .quota_attribution["organization_id"]
        == third.id
    )


def test_failure_after_source_split_rolls_back_all_transfer_changes(
    transfer_case, monkeypatch
):
    from app.services import organization_service

    q = transfer_case

    def fail_after_split(*args, **kwargs):
        assert q.interval.ended_at is not None
        assert q.db.query(QuotaCharge).count() == 1
        raise RuntimeError("injected after source split")

    monkeypatch.setattr(organization_service, "_move_ledger", fail_after_split)
    with pytest.raises(RuntimeError, match="injected"):
        with q.db.begin_nested():
            move(q)
    assert q.owner.organization_id == q.source.id
    assert q.interval.ended_at is None
    assert q.db.query(MeteringInterval).count() == 1
    assert q.db.query(QuotaCharge).count() == 0
    assert q.db.query(QuotaAudit).count() == 0
    assert q.allocation.organization_id == q.reservation.organization_id == q.source.id


@pytest.mark.parametrize("route", ["groups", "users"])
@pytest.mark.parametrize("failure", [AccountingUnavailable, RuntimeError])
def test_both_routes_roll_back_failures_after_source_split(
    transfer_case, monkeypatch, route, failure
):
    from app.api.groups import add_member
    from app.api.users import assign_organization
    from app.schemas.user import OrganizationAssignment
    from app.services import organization_service

    q = transfer_case
    ids = (q.owner.id, q.source.id, q.destination.id, q.project.id)
    q.db.commit()

    def fail_after_split(*args, **kwargs):
        assert q.db.query(QuotaCharge).filter_by(project_id=ids[3]).count() == 1
        raise failure("injected route failure")

    monkeypatch.setattr(organization_service, "_move_ledger", fail_after_split)
    with pytest.raises(
        HTTPException if failure is AccountingUnavailable else RuntimeError
    ) as exc:
        if route == "groups":
            add_member(ids[2], ids[0], q.admin, q.db)
        else:
            assign_organization(
                ids[0], OrganizationAssignment(organization_id=ids[2]), q.admin, q.db
            )
    if failure is AccountingUnavailable:
        assert exc.value.status_code == 503
        assert exc.value.detail == {
            "reason": "accounting_unavailable",
            "message": "injected route failure",
        }
    q.db.expire_all()
    assert q.db.get(User, ids[0]).organization_id == ids[1]
    assert (
        q.db.query(MeteringInterval).filter_by(project_id=ids[3]).one().ended_at is None
    )
    assert q.db.query(QuotaCharge).count() == 0
    assert q.db.query(QuotaAudit).count() == 0
    assert q.db.query(QuotaAllocation).one().organization_id == ids[1]
    assert q.db.query(QuotaReservation).one().organization_id == ids[1]


def test_expected_source_removal_cannot_unassign_a_new_membership(transfer_case):
    q = transfer_case
    move(q)
    with pytest.raises(HTTPException) as exc:
        transfer_user(q.db, q.admin, q.owner.id, None, NOW, expected_source=q.source.id)
    assert exc.value.status_code == 409
    assert q.owner.organization_id == q.destination.id


@pytest.mark.parametrize(
    "dimension,limit", [("cpu", 5), ("storage_bytes", 19), ("projects", 0)]
)
def test_destination_includes_reserved_remainder_and_rejects_without_partial_changes(
    enabled_transfer, dimension, limit
):
    q = enabled_transfer
    q.db.add(
        QuotaPolicy(
            scope="organization", scope_id=q.destination.id, **{dimension: limit}
        )
    )
    q.db.flush()
    with q.db.begin_nested():
        with pytest.raises(QuotaDenied) as exc:
            move(q)
        assert exc.value.dimension == dimension
        # The service checks capacity before splitting or changing anything.
        assert q.owner.organization_id == q.source.id
        assert q.interval.ended_at is None
        assert (
            q.reservation.organization_id == q.allocation.organization_id == q.source.id
        )
        assert q.db.query(QuotaCharge).filter_by(project_id=q.project.id).count() == 0


def test_same_org_is_idempotent_even_if_current_limits_are_below_usage(transfer_case):
    q = transfer_case
    q.db.add(QuotaPolicy(scope="organization", scope_id=q.source.id, cpu=0))
    result = transfer_user(q.db, q.admin, q.owner.id, q.source.id, NOW)
    assert result["changed"] is False
    assert q.interval.ended_at is None
    assert q.db.query(QuotaAudit).filter_by(scope_id=q.owner.id).count() == 0


def test_exhausted_destination_sets_durable_reevaluation_without_rejecting_transfer(
    enabled_transfer,
):
    q = enabled_transfer
    q.db.add(
        QuotaPolicy(
            scope="organization", scope_id=q.destination.id, spending_usd=0, revision=7
        )
    )
    result = move(q)
    assert result["spending_state"] == "budget_exceeded_shutdown_pending"
    marker = next(
        r
        for r in q.db.query(QuotaReadiness).all()
        if r.details.get("scope_id") == q.destination.id
    )
    assert marker.details["policy_revision"] == 7


def test_unassignment_rejects_live_and_pending_even_zero_capacity(transfer_case):
    q = transfer_case
    with pytest.raises(HTTPException) as exc:
        move(q, False)
    assert exc.value.status_code == 409
    q.interval.ended_at = NOW
    q.allocation.state = "released"
    q.allocation.observed = Resources().model_dump()
    q.reservation.requested = q.reservation.observed = Resources().model_dump()
    q.project.state = "draft"
    with pytest.raises(HTTPException):
        move(q, False)


def test_draft_assignment_and_unassignment_work_without_ready_baseline(transfer_case):
    q = transfer_case
    q.db.delete(q.interval)
    q.allocation.state = "released"
    q.allocation.observed = Resources().model_dump()
    q.reservation.state = "confirmed"
    q.reservation.observed = Resources().model_dump()
    q.project.state = "draft"
    q.owner.organization_id = None
    q.db.get(QuotaReadiness, "baseline").ready = False
    assert move(q)["organization_id"] == q.destination.id
    assert move(q, False)["organization_id"] is None


def test_unassignment_accrues_closed_source_and_keeps_retained_attribution(
    transfer_case,
):
    q = transfer_case
    q.interval.ended_at = NOW
    q.allocation.state = "released"
    q.allocation.observed = Resources().model_dump()
    q.reservation.state = "confirmed"
    q.reservation.observed = Resources().model_dump()
    q.project.state = "draft"
    move(q, False)
    assert q.owner.organization_id is None
    assert accounting.charge_rows(q.db, "organization", q.source.id)[0].amount == 6


def test_live_transfer_requires_authoritative_accounting(enabled_transfer):
    q = enabled_transfer
    q.db.get(QuotaReadiness, "baseline").ready = False
    with pytest.raises(AccountingUnavailable):
        move(q)
    assert q.owner.organization_id == q.source.id


def test_backdated_transfer_and_inconsistent_live_attribution_fail_closed(
    transfer_case,
):
    q = transfer_case
    q.allocation.organization_id = q.destination.id
    with pytest.raises(AccountingUnavailable, match="identity"):
        move(q)
    q.allocation.organization_id = q.source.id
    accounting.accrue(q.db, q.project.id, NOW + timedelta(minutes=1))
    with pytest.raises(AccountingUnavailable, match="boundary"):
        move(q)
    assert q.owner.organization_id == q.source.id
    assert q.interval.ended_at is None


def test_transfer_service_is_platform_only_and_destination_must_be_organization(
    transfer_case,
):
    q = transfer_case
    with pytest.raises(HTTPException) as exc:
        transfer_user(q.db, q.manager, q.owner.id, q.destination.id, NOW)
    assert exc.value.status_code == 403
    q.destination.is_organization = False
    with pytest.raises(HTTPException) as exc:
        move(q)
    assert exc.value.status_code == 404


def test_organization_roster_and_billing_access_ignore_stale_reporting_rows(
    transfer_case,
):
    q = transfer_case
    q.db.add_all(
        [
            ManagedGroupMember(group_id=q.source.id, user_id=q.owner.id),
            ManagedGroupManager(group_id=q.source.id, user_id=q.manager.id),
        ]
    )
    q.db.flush()
    move(q)
    assert q.owner.id not in billing_owner_ids(q.db, q.manager)
    assert scoped_members(q.db, q.source.id) == []
    assert [u.id for u in scoped_members(q.db, q.destination.id)] == [q.owner.id]
    with pytest.raises(HTTPException):
        scoped_members(q.db, q.source.id, q.owner.id)
    # Retained legacy roster evidence cannot resurrect revoked access.
    legacy = ManagedGroup(
        name=uuid4().hex, normalized_name=uuid4().hex, is_organization=False
    )
    q.db.add(legacy)
    q.db.flush()
    q.db.add_all(
        [
            ManagedGroupMember(group_id=legacy.id, user_id=q.owner.id),
            ManagedGroupManager(group_id=legacy.id, user_id=q.manager.id),
        ]
    )
    q.db.flush()
    assert q.owner.id not in billing_owner_ids(q.db, q.manager)


def test_org_reports_retain_former_member_costs_but_exclude_other_org_charges(
    transfer_case, monkeypatch
):
    q = transfer_case
    move(q)
    accounting.accrue(q.db, q.project.id, NOW + timedelta(hours=1))
    source = group_report(q.db, q.source, [], 2026, 11, NOW + timedelta(hours=1))
    destination = group_report(
        q.db, q.destination, [q.owner], 2026, 11, NOW + timedelta(hours=1)
    )
    assert Decimal(source["known_subtotal_usd"]) == 6
    assert source["total_usd"] is None
    assert source["active_projects"] == []
    assert Decimal(destination["known_subtotal_usd"]) == 6
    assert destination["total_usd"] is None
    assert Decimal(destination["members"][0]["known_subtotal_usd"]) == 6
    assert Decimal(destination["active_projects"][0]["known_subtotal_usd"]) == 6


def test_destination_existing_usage_is_added_to_all_incoming_capacity(enabled_transfer):
    q = enabled_transfer
    q.db.add_all(
        [
            QuotaPolicy(scope="organization", scope_id=q.destination.id, cpu=10),
            QuotaAllocation(
                operation_id=uuid4().hex,
                project_id=str(uuid4()),
                owner_id=str(uuid4()),
                organization_id=q.destination.id,
                requested={"cpu": 5},
                observed={"cpu": 5},
            ),
        ]
    )
    with pytest.raises(QuotaDenied) as exc:
        move(q)
    assert exc.value.used == 5
    assert exc.value.requested == 6
    assert q.interval.ended_at is None


def test_assignment_endpoint_exposes_org_and_enforces_platform_permission(
    quota_case, api_quota_state
):
    from tests.test_metering_api import _as_user, client

    q = quota_case
    path = f"/api/v1/users/{q.owner.id}/organization"
    _as_user(q.manager)
    assert client.put(path, json={"organization_id": q.org}).status_code == 403
    _as_user(q.admin)
    assert client.put(path, json={"organization_id": q.reporting}).status_code == 404
    assert client.put(path, json={"organization_id": "invalid"}).status_code == 422
    assert (
        client.put(path, json={"organization_id": q.org, "cpu": 999}).status_code == 422
    )
    assert client.put(path, json={"organization_id": None}).status_code == 200
    assert client.put(path, json={"organization_id": q.org}).json()["changed"] is True
    users = client.get("/api/v1/users/").json()
    assert (
        next(user for user in users if user["id"] == q.owner.id)["organization_id"]
        == q.org
    )
    assert [
        user["id"] for user in client.get(f"/api/v1/groups/{q.org}").json()["members"]
    ] == [q.owner.id]
    with TestSession() as db:
        _as_user(db.get(User, q.owner.id))
    assert client.get("/api/v1/auth/me").json()["organization_id"] == q.org


@pytest.mark.parametrize("route", ["groups", "users"])
def test_assignment_endpoint_denial_rolls_back_membership_and_returns_binding_capacity(
    quota_case, api_quota_state, monkeypatch, route
):
    from tests.test_groups_api import make_group
    from tests.test_metering_api import _as_user, client

    q = quota_case
    destination = make_group(q.admin, q.manager, q.other)
    with TestSession.begin() as db:
        db.get(ManagedGroup, destination).is_organization = True
        db.add(QuotaPolicy(scope="organization", scope_id=destination, cpu=0))
        db.add(
            QuotaReservation(
                operation_id=uuid4().hex,
                project_id=q.pid,
                owner_id=q.owner.id,
                organization_id=q.org,
                requested={"cpu": 1},
                observed={},
                state="pending",
            )
        )
        db.merge(
            QuotaReadiness(
                id="baseline",
                ready=True,
                baseline_version="operator-baseline-v2-allocated",
                details={"accounting_ready": True},
            )
        )
    _as_user(q.admin)
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    response = (
        client.put(
            f"/api/v1/users/{q.owner.id}/organization",
            json={"organization_id": destination},
        )
        if route == "users"
        else client.put(f"/api/v1/groups/{destination}/members/{q.owner.id}")
    )
    assert response.status_code == 409
    assert response.json()["detail"]["dimension"] == "cpu"
    with TestSession() as db:
        assert db.get(User, q.owner.id).organization_id == q.org
        assert (
            not db.query(QuotaAudit)
            .filter_by(scope_id=q.owner.id, action="organization_transfer")
            .first()
        )


def test_retiring_empty_organization_preserves_released_ledger_and_charge_identity(
    quota_case, api_quota_state
):
    from tests.test_groups_api import make_group
    from tests.test_metering_api import _as_user, client

    q = quota_case
    organization = make_group(q.admin, q.manager, q.other)
    operation = uuid4().hex
    with TestSession.begin() as db:
        db.get(User, q.other.id).organization_id = None
        db.get(ManagedGroup, organization).is_organization = True
        db.add(
            QuotaAllocation(
                operation_id=operation,
                project_id=q.pid,
                owner_id=q.owner.id,
                organization_id=organization,
                requested={},
                observed={},
                state="released",
            )
        )
        db.add(
            QuotaReservation(
                operation_id=operation,
                project_id=q.pid,
                owner_id=q.owner.id,
                organization_id=organization,
                requested={},
                observed={},
                state="confirmed",
            )
        )
        db.add(
            QuotaCharge(
                project_id=q.pid,
                project_name="historic",
                owner_id=q.owner.id,
                owner_name="historic",
                organization_id=organization,
                organization_name="retired",
                source="interval",
                source_id=operation,
                start_at=NOW - timedelta(hours=1),
                end_at=NOW,
                kind="vcpu",
                quantity=1,
                rate=2,
                amount=2,
            )
        )
    _as_user(q.admin)
    assert client.delete(f"/api/v1/groups/{organization}").status_code == 204
    with TestSession() as db:
        assert (
            db.query(QuotaCharge)
            .filter_by(organization_id=organization)
            .one()
            .organization_name
            == "retired"
        )
        assert (
            db.query(QuotaAllocation)
            .filter_by(operation_id=operation)
            .one()
            .organization_id
            == organization
        )


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_opposite_transfers_serialize_without_deadlock_and_move_pending_work(
    postgres, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services.quota_admission import reserve, usage

    sessions, (projects, users, source, destination), _ = postgres
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    with sessions.begin() as db:
        db.get(User, users[1]).organization_id = destination
        db.query(QuotaPolicy).filter_by(
            scope="organization", scope_id=source
        ).one().cpu = 12
        db.add(QuotaPolicy(scope="organization", scope_id=destination, cpu=12))
        for i in range(2):
            reserve(db, projects[i], f"opposite-{i}", Resources(cpu=6), NOW)
    barrier = Barrier(2)

    def transfer(index):
        with sessions.begin() as db:
            barrier.wait(timeout=5)
            return transfer_user(
                db,
                db.get(User, users[index]),
                users[index],
                [destination, source][index],
                NOW,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(transfer, i) for i in range(2)]
        assert all(future.result(timeout=10)["changed"] for future in futures)
    with sessions() as db:
        assert [db.get(User, user).organization_id for user in users] == [
            destination,
            source,
        ]
        assert (
            usage(db, "organization", source)[1].cpu
            == usage(db, "organization", destination)[1].cpu
            == 6
        )
        assert {
            r.operation_id: r.organization_id for r in db.query(QuotaReservation)
        } == {"opposite-0": destination, "opposite-1": source}


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_transfer_competes_atomically_with_another_users_admission(
    postgres, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services.quota_admission import reserve, usage

    sessions, (projects, users, source, destination), _ = postgres
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    with sessions.begin() as db:
        db.get(User, users[1]).organization_id = destination
        db.add(QuotaPolicy(scope="organization", scope_id=destination, cpu=10))
        reserve(db, projects[0], "moving-six", Resources(cpu=6), NOW)
    barrier = Barrier(2)

    def compete(transfer):
        with sessions() as db:
            barrier.wait(timeout=5)
            try:
                if transfer:
                    transfer_user(
                        db, db.get(User, users[0]), users[0], destination, NOW
                    )
                else:
                    reserve(db, projects[1], "new-six", Resources(cpu=6), NOW)
                db.commit()
                return "admitted"
            except QuotaDenied:
                db.rollback()
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compete, transfer) for transfer in (True, False)]
        assert sorted(future.result(timeout=10) for future in futures) == [
            "admitted",
            "denied",
        ]
    with sessions() as db:
        assert usage(db, "organization", destination)[1].cpu == 6
        assert usage(db, "user", users[0])[1].cpu == 6


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_cached_confirmation_refreshes_transferred_attribution_and_preserves_claim(
    postgres,
    monkeypatch,
):
    from app.services.quota_admission import confirm, reserve

    sessions, (projects, users, _, destination), _ = postgres
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    with sessions.begin() as db:
        reserve(db, projects[0], "before-transfer", Resources(cpu=6), NOW)
        row = db.query(QuotaReservation).one()
        row.dispatch_metadata = {"generation_id": "generation", "claim": "claim"}
    with sessions() as confirmer:
        retained = confirmer.query(QuotaReservation).one()
        with sessions.begin() as transferring:
            transfer_user(
                transferring,
                transferring.get(User, users[0]),
                users[0],
                destination,
                NOW,
            )
        confirm(
            confirmer,
            retained.operation_id,
            Resources(cpu=6),
            "after-transfer",
            NOW + timedelta(seconds=1),
        )
        confirmer.commit()
        assert retained.organization_id == destination
        allocation = confirmer.query(QuotaAllocation).one()
        assert allocation.organization_id == destination
        assert retained.dispatch_metadata == {
            "generation_id": "generation",
            "claim": "claim",
        }


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_actual_transfer_fences_admission_resolved_before_membership_changed(
    postgres, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    from app.services.quota_admission import reserve
    from app.services.quota_locks import ScopeChanged

    sessions, (projects, users, _, destination), engine = postgres
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    resolving, transferred = Event(), Event()
    with sessions.begin() as db:
        db.add(QuotaPolicy(scope="organization", scope_id=destination, cpu=5))

    def pause_before_user_lock(
        conn, cursor, statement, parameters, context, executemany
    ):
        if (
            "FROM users" in statement
            and "FOR UPDATE" in statement
            and not resolving.is_set()
        ):
            resolving.set()
            assert transferred.wait(timeout=5)

    event.listen(engine, "before_cursor_execute", pause_before_user_lock)

    def admission():
        with sessions() as db:
            with pytest.raises(ScopeChanged):
                reserve(db, projects[0], "stale-parent", Resources(cpu=6), NOW)
            db.rollback()
            with pytest.raises(QuotaDenied) as exc:
                reserve(db, projects[0], "stale-parent", Resources(cpu=6), NOW)
            assert exc.value.scope_id == destination
            assert exc.value.limit == 5
            db.rollback()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(admission)
            assert resolving.wait(timeout=5)
            with sessions.begin() as db:
                transfer_user(db, db.get(User, users[0]), users[0], destination, NOW)
            transferred.set()
            future.result(timeout=10)
        with sessions() as db:
            assert not db.query(QuotaReservation).first()
    finally:
        transferred.set()
        event.remove(engine, "before_cursor_execute", pause_before_user_lock)


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_disabled_transfer_checks_stale_removal_after_waiting_for_user_lock(
    postgres, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident

    from sqlalchemy import event

    sessions, (_, users, source, destination), engine = postgres
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    attempted, finished = Event(), Event()
    worker_ids = []

    def deleting():
        worker_ids.append(get_ident())
        with sessions() as db:
            cached = db.get(User, users[0])
            assert cached.organization_id == source
            try:
                transfer_user(db, cached, users[0], None, NOW, expected_source=source)
                db.commit()
                return "removed"
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
            finally:
                finished.set()

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            worker_ids
            and get_ident() == worker_ids[0]
            and "FROM users" in statement
            and "FOR UPDATE" in statement
        ):
            attempted.set()

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with sessions() as db, ThreadPoolExecutor(max_workers=1) as pool:
            transfer_user(db, db.get(User, users[0]), users[0], destination, NOW)
            pending = pool.submit(deleting)
            try:
                assert attempted.wait(3)
                assert not finished.wait(0.2)
                db.commit()
            finally:
                db.rollback()
            assert pending.result(timeout=5) == 409
        with sessions() as db:
            assert db.get(User, users[0]).organization_id == destination
            assert (
                db.query(QuotaAudit).filter_by(action="organization_transfer").count()
                == 1
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_disabled_transfer_splits_sources_atomically_with_accrual(
    postgres, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident

    from sqlalchemy import event

    from app.services.quota_locks import ScopeChanged

    sessions, (projects, users, source, destination), engine = postgres
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    with sessions.begin() as db:
        db.get(QuotaReadiness, "baseline").ready = False
        project = db.get(Project, projects[0])
        project.state = "running"
        db.add(
            MeteringInterval(
                project_id=project.id,
                kind="vcpu",
                resource_id="vm",
                qty=2,
                unit_rate=3,
                started_at=NOW - timedelta(hours=1),
                quota_attribution=accounting.attribution(db, project),
            )
        )
    attempted, finished = Event(), Event()
    worker_ids = []

    def accruing():
        worker_ids.append(get_ident())
        with sessions() as db:
            try:
                accounting.accrue(db, projects[0], NOW + timedelta(hours=1))
                db.commit()
                return "unexpectedly committed"
            except ScopeChanged:
                db.rollback()
                accounting.accrue(db, projects[0], NOW + timedelta(hours=1))
                db.commit()
                return "retried"
            finally:
                finished.set()

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if worker_ids and get_ident() == worker_ids[0] and "FOR UPDATE" in statement:
            attempted.set()

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with sessions() as db, ThreadPoolExecutor(max_workers=1) as pool:
            result = transfer_user(
                db, db.get(User, users[0]), users[0], destination, NOW
            )
            assert result["enforcement_state"] == "inactive"
            pending = pool.submit(accruing)
            try:
                assert attempted.wait(3)
                assert not finished.wait(0.2)
                db.commit()
            finally:
                db.rollback()
            assert pending.result(timeout=5) == "retried"
        with sessions() as db:
            assert db.get(QuotaReadiness, "baseline").ready is False
            assert {r.organization_id: r.amount for r in db.query(QuotaCharge)} == {
                source: Decimal(6),
                destination: Decimal(6),
            }
            assert (
                db.query(QuotaAllocation).count()
                == db.query(QuotaReservation).count()
                == 0
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
