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
def transfer_case(monkeypatch):
    db = TestSession()
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


@pytest.mark.parametrize(
    "dimension,limit", [("cpu", 5), ("storage_bytes", 19), ("projects", 0)]
)
def test_destination_includes_reserved_remainder_and_rejects_without_partial_changes(
    transfer_case, dimension, limit
):
    q = transfer_case
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
    transfer_case,
):
    q = transfer_case
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


def test_live_transfer_requires_authoritative_accounting(transfer_case):
    q = transfer_case
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
    # An intentionally independent reporting group retains the legacy authority.
    legacy = ManagedGroup(name=uuid4().hex, normalized_name=uuid4().hex)
    q.db.add(legacy)
    q.db.flush()
    q.db.add_all(
        [
            ManagedGroupMember(group_id=legacy.id, user_id=q.owner.id),
            ManagedGroupManager(group_id=legacy.id, user_id=q.manager.id),
        ]
    )
    q.db.flush()
    assert q.owner.id in billing_owner_ids(q.db, q.manager)


def test_org_reports_retain_former_member_costs_but_exclude_other_org_charges(
    transfer_case, monkeypatch
):
    q = transfer_case
    move(q)
    accounting.accrue(q.db, q.project.id, NOW + timedelta(hours=1))
    monkeypatch.setattr("app.services.group_metering.quotas_enabled", lambda: True)
    source = group_report(q.db, q.source, [], 2026, 11, NOW + timedelta(hours=1))
    destination = group_report(
        q.db, q.destination, [q.owner], 2026, 11, NOW + timedelta(hours=1)
    )
    assert Decimal(source["total_usd"]) == 6
    assert source["active_projects"] == []
    assert Decimal(destination["total_usd"]) == 6
    assert Decimal(destination["members"][0]["total_usd"]) == 6
    assert Decimal(destination["active_projects"][0]["total_usd"]) == 6


def test_destination_existing_usage_is_added_to_all_incoming_capacity(transfer_case):
    q = transfer_case
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


def test_assignment_endpoint_denial_rolls_back_membership_and_returns_binding_capacity(
    quota_case, api_quota_state
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
    response = client.put(
        f"/api/v1/users/{q.owner.id}/organization",
        json={"organization_id": destination},
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
def test_opposite_transfers_serialize_without_deadlock_and_move_pending_work(postgres):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services.quota_admission import reserve, usage

    sessions, (projects, users, source, destination), _ = postgres
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
def test_transfer_competes_atomically_with_another_users_admission(postgres):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services.quota_admission import reserve, usage

    sessions, (projects, users, source, destination), _ = postgres
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
):
    from app.services.quota_admission import confirm, reserve

    sessions, (projects, users, _, destination), _ = postgres
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
def test_actual_transfer_fences_admission_resolved_before_membership_changed(postgres):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    from app.services.quota_admission import reserve
    from app.services.quota_locks import ScopeChanged

    sessions, (projects, users, _, destination), engine = postgres
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
