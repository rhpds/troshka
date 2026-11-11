from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from app.models.managed_group import ManagedGroup
from app.models.project import Project
from app.models.quota import (
    QuotaAction,
    QuotaAllocation,
    QuotaCharge,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
)
from app.models.user import User
from app.schemas.quota import Resources
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_enforcement as enforcement
from tests import conftest

NOW = datetime(2026, 11, 30, 23, 59, tzinfo=UTC)
REAL_SPEND = accounting.spend


@pytest.fixture
def ledger(monkeypatch, isolated_quota_database):
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(enforcement, "_now", lambda: NOW)
    with conftest.TestSession() as db:
        baseline = db.get(QuotaReadiness, "baseline")
        previous = (
            {
                name: getattr(baseline, name)
                for name in ("ready", "details", "checked_at", "baseline_version")
            }
            if baseline
            else None
        )
        org = ManagedGroup(
            name=str(uuid4()), normalized_name=str(uuid4()), is_organization=True
        )
        db.add(org)
        db.flush()
        user = User(email=f"{uuid4()}@test.invalid", organization_id=org.id)
        db.add(user)
        db.flush()
        project = Project(
            name="budget", owner_id=user.id, state="draft", budget_usd=Decimal(100)
        )
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
        amounts = {
            "organization": Decimal(0),
            "user": Decimal(0),
            "project": Decimal(0),
        }
        # This fixture's controlled spending amounts belong only to its owner.
        # Keep other modules' retained projects out of that synthetic inventory;
        # per-project execution and the PostgreSQL full-inventory tests stay real.
        snapshot = enforcement._snapshot

        def scoped_snapshot(session, project_id=None):
            rows = snapshot(session, project_id)
            return (
                rows
                if project_id is not None
                else [row for row in rows if row.owner_id == user.id]
            )

        monkeypatch.setattr(enforcement, "_snapshot", scoped_snapshot)
        monkeypatch.setattr(
            accounting, "spend", lambda db, scope, identity, now: amounts[scope]
        )
        yield db, project, user, org, amounts
        db.rollback()
        for model in (QuotaAction, QuotaAllocation, QuotaReservation, QuotaCharge):
            db.query(model).filter(model.project_id == project.id).delete(
                synchronize_session=False
            )
        db.query(QuotaAction).filter(
            QuotaAction.scope_id.in_((user.id, org.id, project.id))
        ).delete(synchronize_session=False)
        db.query(QuotaPolicy).filter(
            QuotaPolicy.scope_id.in_((user.id, org.id, project.id))
        ).delete(synchronize_session=False)
        for marker in db.query(QuotaReadiness).all():
            if marker.id != "baseline" and (
                (marker.details or {}).get("project_id") == project.id
                or marker.id
                == str(uuid5(NAMESPACE_URL, f"troshka:enforcement:{project.id}"))
            ):
                db.delete(marker)
        baseline = db.get(QuotaReadiness, "baseline")
        if previous and baseline:
            for name, value in previous.items():
                setattr(baseline, name, value)
        elif baseline:
            db.delete(baseline)
        db.query(Project).filter_by(id=project.id).delete(synchronize_session=False)
        db.query(User).filter_by(id=user.id).delete(synchronize_session=False)
        db.query(ManagedGroup).filter_by(id=org.id).delete(synchronize_session=False)
        db.commit()


def actions(db, project, kind):
    return db.query(QuotaAction).filter_by(project_id=project.id, kind=kind).all()


def test_durable_thresholds_deduplicate_and_revision_month_reset(ledger):
    db, project, user, org, amounts = ledger
    db.add_all(
        [
            QuotaPolicy(scope="organization", scope_id=org.id, spending_usd=100),
            QuotaPolicy(scope="user", scope_id=user.id, spending_usd=100),
        ]
    )
    amounts.update(dict.fromkeys(amounts, Decimal(90)))
    enforcement.evaluate(db, NOW)
    enforcement.evaluate(db, NOW)
    warnings = db.query(QuotaAction).filter_by(kind="warning").all()
    assert len(warnings) == 6
    assert {row.threshold for row in warnings} == {80, 90}
    policy = db.query(QuotaPolicy).filter_by(scope="user", scope_id=user.id).one()
    policy.revision = 2
    enforcement.evaluate(db, NOW)
    assert db.query(QuotaAction).filter_by(kind="warning").count() == 8
    enforcement.evaluate(db, NOW + timedelta(minutes=2))
    assert db.query(QuotaAction).filter_by(kind="warning").count() == 12


def test_zero_unlimited_multiple_causes_and_stale_queued_cancellation(ledger):
    db, project, user, org, amounts = ledger
    project.budget_usd = None
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, spending_usd=0))
    enforcement.evaluate(db, NOW)
    assert len(actions(db, project, "stop")) == 1
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    assert len(actions(db, project, "stop")) == 2
    assert len(actions(db, project, "stop_run")) == 1
    db.query(QuotaPolicy).filter_by(
        scope="organization", scope_id=org.id
    ).one().spending_usd = None
    project.budget_usd = None
    enforcement.reconcile_action(db, actions(db, project, "stop_run")[0].id, NOW)
    assert all(row.state == "cancelled" for row in actions(db, project, "stop"))


def test_monthly_reset_cancels_but_project_lifetime_still_exhausted(ledger):
    db, project, user, org, amounts = ledger
    db.add(QuotaPolicy(scope="user", scope_id=user.id, spending_usd=1))
    amounts["user"] = amounts["project"] = Decimal(100)
    enforcement.evaluate(db, NOW)
    amounts["user"] = Decimal(0)
    enforcement.evaluate(db, NOW + timedelta(minutes=2))
    live = [a for a in actions(db, project, "stop") if a.state != "cancelled"]
    assert [a.scope for a in live] == ["project"]


def test_unavailable_accounting_does_not_create_shutdown(ledger, monkeypatch):
    db, project, user, org, amounts = ledger

    def unavailable(*args):
        raise accounting.AccountingUnavailable("offline")

    monkeypatch.setattr(accounting, "spend", unavailable)
    enforcement.evaluate(db, NOW)
    assert not actions(db, project, "stop")
    assert (
        enforcement.project_status(db, project.id)["accounting_status"] == "unavailable"
    )


def test_fence_provisioning_and_retry_failed_stop_without_releasing_storage(
    ledger, monkeypatch
):
    db, project, user, org, amounts = ledger
    project.budget_usd = 0
    row = QuotaReservation(
        operation_id=str(uuid4()),
        project_id=project.id,
        owner_id=user.id,
        organization_id=org.id,
        requested={"cpu": 2, "storage_bytes": 10},
        dispatch_metadata={"claim": "worker", "worker_finished": False},
    )
    db.add(row)
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    monkeypatch.setattr(
        enforcement, "_dispatch", Mock(side_effect=RuntimeError("stop failed"))
    )
    enforcement.reconcile_action(db, run.id, NOW)
    assert row.dispatch_metadata["cancelled"] is True
    assert run.state == "failed" and run.attempts == 1
    assert accounting.utc(run.next_attempt_at) > NOW
    enforcement.reconcile_action(db, run.id, NOW + timedelta(seconds=1))
    assert run.attempts == 1
    monkeypatch.setattr(enforcement, "_dispatch", Mock())
    enforcement.reconcile_action(db, run.id, NOW + timedelta(minutes=5))
    assert run.attempts == 2 and run.state == "issued"
    assert row.requested["storage_bytes"] == 10


def test_force_stop_override_uses_stop_and_preserves_preference(ledger, monkeypatch):
    from app.models.host import Host
    from app.services import deploy_service

    db, project, user, org, amounts = ledger
    host = Host(ip_address="192.0.2.1", host_type="shared")
    db.add(host)
    db.flush()
    project.host_id = host.id
    project.off_action = "pause"
    project.topology = {"nodes": [{"id": "vm", "type": "vmNode", "data": {}}]}
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: db)
    stop = Mock(return_value={"vm"})
    monkeypatch.setattr(deploy_service, "_apply_off_action_troshkad", stop)
    monkeypatch.setattr(deploy_service, "_finalize_project_stopped", Mock())
    monkeypatch.setattr(db, "close", lambda: None)
    deploy_service.stop_project_async(project.id, force_stop=True)
    assert stop.call_args.args[-1] == "stop"
    assert project.off_action == "pause"


def compute(db, project, user, org, *, finished=True):
    project.state = "active"
    project.topology = {
        "nodes": [{"id": "vm", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}}]
    }
    vector = Resources(cpu=2, ram_bytes=4 * 2**30, storage_bytes=10, projects=1)
    row = QuotaReservation(
        operation_id=str(uuid4()),
        project_id=project.id,
        owner_id=user.id,
        organization_id=org.id,
        requested=vector.model_dump(),
        dispatch_metadata={
            "claim": "worker",
            "generation_id": str(uuid4()),
            "worker_finished": finished,
            "worker_finished_at": (NOW - timedelta(minutes=1)).isoformat(),
        },
    )
    db.add(row)
    db.flush()
    admission.confirm(
        db, row.operation_id, vector, "initial", NOW - timedelta(minutes=1)
    )
    return row


def test_confirm_only_fresh_off_after_settled_growth_retains_storage(
    ledger, monkeypatch
):
    db, project, user, org, amounts = ledger
    row = compute(db, project, user, org, finished=False)
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    snapshot = {"observed_at": NOW - timedelta(seconds=1), "states": {"vm": "off"}}
    monkeypatch.setattr(
        "app.services.ws_pubsub.get_cached_vm_states", lambda pid: snapshot
    )

    def stop(pid):
        assert (
            not db.in_transaction()
        ), "provider stop must execute after row-lock transaction commits"

    dispatch = Mock(side_effect=stop)
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.reconcile_action(db, run.id, NOW)
    assert row.dispatch_metadata["cancelled"] and run.state == "issued"
    assert row.observed["cpu"] == 2
    snapshot["observed_at"] = NOW + timedelta(seconds=10)
    enforcement.reconcile_action(db, run.id, NOW + timedelta(seconds=10))
    assert run.state == "issued" and row.observed["cpu"] == 2
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "worker_finished": True,
        "worker_finished_at": (NOW + timedelta(seconds=15)).isoformat(),
    }
    enforcement.reconcile_action(db, run.id, NOW + timedelta(seconds=16))
    assert run.state == "issued" and row.observed["cpu"] == 2
    snapshot["observed_at"] = NOW + timedelta(seconds=20)
    enforcement.reconcile_action(db, run.id, NOW + timedelta(seconds=20))
    assert (
        run.state == "completed"
        and row.observed == Resources(storage_bytes=10, projects=1).model_dump()
    )
    assert dispatch.call_count == 1


def test_real_decimal_month_reset_preserves_project_lifetime(ledger, monkeypatch):
    db, project, user, org, amounts = ledger
    monkeypatch.setattr(accounting, "spend", REAL_SPEND)
    db.add_all(
        [
            QuotaPolicy(scope="organization", scope_id=org.id, spending_usd=100),
            QuotaPolicy(scope="user", scope_id=user.id, spending_usd=100),
        ]
    )
    db.add(
        QuotaCharge(
            project_id=project.id,
            project_name=project.name,
            owner_id=user.id,
            organization_id=org.id,
            source="test",
            source_id=str(uuid4()),
            kind="compute",
            start_at=NOW - timedelta(seconds=30),
            end_at=NOW,
            quantity=1,
            rate=0,
            amount=Decimal("100.000000000000"),
        )
    )
    enforcement.evaluate(db, NOW)
    assert len(actions(db, project, "stop")) == 3
    enforcement.evaluate(db, NOW + timedelta(minutes=2))
    active = [a.scope for a in actions(db, project, "stop") if a.state != "cancelled"]
    assert active == ["project"]


def test_warning_delivery_uses_pubsub_and_durable_dedup(ledger, monkeypatch):
    db, project, user, org, amounts = ledger
    amounts["project"] = Decimal(80)
    enforcement.evaluate(db, NOW)
    notify = Mock()
    monkeypatch.setattr("app.services.ws_pubsub.notify_project", notify)
    enforcement._deliver_warnings(db)
    enforcement.evaluate(db, NOW)
    enforcement._deliver_warnings(db)
    assert notify.call_count == 1
    assert notify.call_args.args[1]["threshold"] == 80
    assert notify.call_args.args[1]["event_id"]


def test_accounting_loss_after_queued_cause_does_not_issue_stop(ledger, monkeypatch):
    db, project, user, org, amounts = ledger
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)

    def unavailable(*args):
        raise accounting.AccountingUnavailable("offline")

    monkeypatch.setattr(accounting, "spend", unavailable)
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.reconcile_action(db, actions(db, project, "stop_run")[0].id, NOW)
    dispatch.assert_not_called()


def test_legacy_budget_bypassed_only_when_enabled(ledger, monkeypatch):
    from app.services.metering_service import apply_budget

    db, project, user, org, amounts = ledger
    project.state = "active"
    spawn = Mock()
    monkeypatch.setattr("app.services.project_timer._spawn_stop", spawn)
    monkeypatch.setattr("app.services.metering_service.quotas_enabled", lambda: True)
    apply_budget(project, 100)
    spawn.assert_not_called()
    monkeypatch.setattr("app.services.metering_service.quotas_enabled", lambda: False)
    apply_budget(project, 100)
    spawn.assert_called_once_with(project.id)


def test_completed_coordinator_rearms_for_new_compute_under_same_cause(ledger):
    db, project, user, org, amounts = ledger
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    cause = actions(db, project, "stop")[0]
    run.state = cause.state = "completed"
    run.details = {"cause_ids": [cause.id]}
    compute(db, project, user, org)
    enforcement.evaluate(db, NOW)
    assert run.state == "pending"


def test_never_dispatched_empty_draft_is_fenced_and_completed_without_provider(
    ledger, monkeypatch
):
    db, project, user, org, amounts = ledger
    project.budget_usd = 0
    row = QuotaReservation(
        operation_id=str(uuid4()),
        project_id=project.id,
        owner_id=user.id,
        organization_id=org.id,
        requested={"cpu": 2, "storage_bytes": 10},
        dispatch_metadata={"known_empty": True},
    )
    db.add(row)
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.reconcile_action(db, run.id, NOW)
    assert run.state == "completed"
    assert row.dispatch_metadata["cancelled"]
    assert not any(row.observed.values())
    dispatch.assert_not_called()


def test_warning_retry_and_lost_delivery_claim_are_durable(ledger, monkeypatch):
    db, project, user, org, amounts = ledger
    amounts["project"] = Decimal(80)
    enforcement.evaluate(db, NOW)
    notify = Mock(side_effect=RuntimeError("offline"))
    monkeypatch.setattr("app.services.ws_pubsub.notify_project", notify)
    enforcement._deliver_warnings(db)
    warning = db.query(QuotaAction).filter_by(kind="warning").one()
    assert warning.state == "failed" and warning.attempts == 1
    warning.state = "issued"  # A previous delivery process died after its claim.
    warning.next_attempt_at = NOW - timedelta(seconds=1)
    monkeypatch.setattr("app.services.ws_pubsub.notify_project", Mock())
    enforcement._deliver_warnings(db)
    assert warning.state == "completed" and warning.attempts == 2


def test_policy_reduction_uses_current_spend_and_increase_cancels_queued_stop(
    ledger, monkeypatch
):
    from app.schemas.quota import Limits
    from app.services.quota_policy import set_limits

    db, project, user, org, amounts = ledger
    amounts["project"] = Decimal(75)
    enforcement.evaluate(db, NOW)
    assert not actions(db, project, "stop")
    set_limits(db, user, "project", project.id, Limits(spending_usd=50), NOW)
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    set_limits(db, user, "project", project.id, Limits(spending_usd=200), NOW)
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.reconcile_action(db, run.id, NOW)
    dispatch.assert_not_called()
    assert run.state == "cancelled"


def test_membership_transfer_rechecks_queued_cause_and_destination_handoff(
    ledger, monkeypatch
):
    from app.services.organization_service import transfer_user

    db, project, user, org, amounts = ledger
    user.role = "admin"
    destination = ManagedGroup(
        name=str(uuid4()), normalized_name=str(uuid4()), is_organization=True
    )
    db.add(destination)
    db.flush()
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, spending_usd=1))
    project.budget_usd = None
    monkeypatch.setattr(
        accounting,
        "spend",
        lambda db, scope, identity, now: Decimal(1)
        if identity == org.id
        else Decimal(0),
    )
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    transfer_user(db, user, user.id, destination.id, NOW)
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.reconcile_action(db, run.id, NOW)
    dispatch.assert_not_called()
    assert run.state == "cancelled"
    db.add(QuotaPolicy(scope="organization", scope_id=destination.id, spending_usd=0))
    result = transfer_user(db, user, user.id, org.id, NOW)
    assert result["spending_state"] == "budget_exceeded_shutdown_pending"
    enforcement.evaluate(db, NOW)
    assert run.state == "pending"
    assert (
        enforcement.project_status(db, project.id)["spending_state"]
        == "budget_exceeded_shutdown_pending"
    )
    # The test commits during reconciliation; explicitly remove its extra identity.
    db.query(QuotaPolicy).filter_by(scope_id=destination.id).delete(
        synchronize_session=False
    )
    for marker in db.query(QuotaReadiness).all():
        if (marker.details or {}).get("scope_id") == destination.id:
            db.delete(marker)
    db.query(ManagedGroup).filter_by(id=destination.id).delete(
        synchronize_session=False
    )
    db.commit()


def test_disabled_periodic_evaluator_does_no_database_work(monkeypatch):
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    session = Mock(side_effect=AssertionError("disabled evaluator must not access DB"))
    monkeypatch.setattr("app.core.database.SessionLocal", session)
    enforcement.run_once()
    session.assert_not_called()
    thread = Mock()
    monkeypatch.setattr(enforcement.threading, "Thread", thread)
    enforcement.start_enforcement_poll()
    thread.assert_not_called()


def test_failed_stop_error_remains_retryable_with_durable_exhaustion_proof(
    ledger, monkeypatch
):
    db, project, user, org, amounts = ledger
    compute(db, project, user, org)
    amounts["project"] = Decimal(100)
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]

    def fail(pid):
        project.state = "error"
        raise RuntimeError("stop failed")

    monkeypatch.setattr(enforcement, "_dispatch", fail)
    enforcement.reconcile_action(db, run.id, NOW)

    def unavailable(*args):
        raise accounting.AccountingUnavailable("error requires reconciliation")

    monkeypatch.setattr(accounting, "spend", unavailable)
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.reconcile_action(db, run.id, NOW + timedelta(minutes=1))
    dispatch.assert_called_once_with(project.id)
    assert run.attempts == 2 and run.state == "failed"


@pytest.mark.parametrize("change", ["increase", "revision", "month", "membership"])
def test_unavailable_accounting_cannot_keep_invalidated_exhaustion_proof(
    ledger, monkeypatch, change
):
    db, project, user, org, amounts = ledger
    project.budget_usd = None
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, spending_usd=100))
    amounts["organization"] = Decimal(100)
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    policy = (
        db.query(QuotaPolicy).filter_by(scope="organization", scope_id=org.id).one()
    )
    when = NOW
    if change == "increase":
        policy.spending_usd = 200
    elif change == "revision":
        policy.revision += 1
    elif change == "month":
        when += timedelta(minutes=2)
    else:
        user.organization_id = None

    def unavailable(*args):
        raise accounting.AccountingUnavailable("offline")

    monkeypatch.setattr(accounting, "spend", unavailable)
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.reconcile_action(db, run.id, when)
    dispatch.assert_not_called()
    assert run.state == "cancelled"


def test_stop_service_error_return_is_visible_failed_not_success(ledger, monkeypatch):
    db, project, user, org, amounts = ledger
    compute(db, project, user, org)
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]

    def failed_service(pid):
        project.state = "error"  # Existing stop service catches its own errors.

    monkeypatch.setattr(enforcement, "_dispatch", failed_service)
    enforcement.reconcile_action(db, run.id, NOW)
    assert run.state == "failed" and run.last_error


def test_simultaneous_causes_dispatch_once_and_reset_never_restarts(
    ledger, monkeypatch
):
    db, project, user, org, amounts = ledger
    compute(db, project, user, org)
    project.budget_usd = None
    db.add_all(
        [
            QuotaPolicy(scope="organization", scope_id=org.id, spending_usd=100),
            QuotaPolicy(scope="user", scope_id=user.id, spending_usd=100),
        ]
    )
    amounts.update(dict.fromkeys(amounts, Decimal(100)))
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    stop, start = Mock(), Mock()
    monkeypatch.setattr(enforcement, "_dispatch", stop)
    monkeypatch.setattr("app.services.deploy_service.start_project_async", start)
    enforcement.reconcile_action(db, run.id, NOW)
    enforcement.reconcile_action(db, run.id, NOW)
    stop.assert_called_once_with(project.id)
    assert len(run.details["cause_ids"]) == 2
    project.state = "stopped"
    amounts.update(dict.fromkeys(amounts, Decimal(0)))
    enforcement.evaluate(db, NOW + timedelta(minutes=2))
    enforcement.reconcile_action(db, run.id, NOW + timedelta(minutes=2))
    assert run.state == "cancelled" and project.state == "stopped"
    start.assert_not_called()
    stop.assert_called_once()


def test_periodic_delivery_is_durable_async_and_preserves_deploy_job_reference(
    ledger, monkeypatch
):
    db, project, user, org, amounts = ledger
    compute(db, project, user, org)
    project.budget_usd = 0
    db.commit()
    queued = []

    def enqueue(function, action_id, **kwargs):
        assert "project_id" not in kwargs  # Never overwrite in-flight deploy tracking.
        with conftest.TestSession() as observer:
            assert observer.get(QuotaAction, action_id).kind == "stop_run"
        queued.append((function, action_id))

    monkeypatch.setattr("app.core.redis.enqueue_job", enqueue)
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.run_once()
    assert len(queued) == 1
    dispatch.assert_not_called()
    function, action_id = queued[0]
    function(action_id)
    function(action_id)  # Duplicate/redelivered queue messages share a durable claim.
    dispatch.assert_called_once_with(project.id)


def test_delivered_worker_rechecks_allowance_after_queue_delay(ledger, monkeypatch):
    db, project, user, org, amounts = ledger
    compute(db, project, user, org)
    project.budget_usd = 0
    db.commit()
    queued = []
    monkeypatch.setattr(
        "app.core.redis.enqueue_job", lambda fn, identity: queued.append((fn, identity))
    )
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.run_once()
    project.budget_usd = None
    db.commit()
    function, action_id = queued[0]
    function(action_id)
    dispatch.assert_not_called()
    db.expire_all()
    assert db.get(QuotaAction, action_id).state == "cancelled"


def test_budget_control_fences_growth_before_occupied_queue_runs(ledger, monkeypatch):
    from types import SimpleNamespace

    from app.services import quota_operations

    db, project, user, org, amounts = ledger
    row = compute(db, project, user, org, finished=False)
    row.state = "dispatched"
    project.state = "deploying"
    project.budget_usd = 100
    amounts["project"] = Decimal(100)
    db.commit()
    queued = []
    monkeypatch.setattr(
        "app.core.redis.enqueue_job", lambda fn, identity: queued.append((fn, identity))
    )
    notification_fences = []

    def notification(pid, message):
        with conftest.TestSession() as observer:
            current = observer.get(QuotaReservation, row.id)
            notification_fences.append(
                current.dispatch_metadata.get("cancelled", False)
            )

    monkeypatch.setattr("app.services.ws_pubsub.notify_project", notification)
    enforcement.run_once()  # The lifecycle queue is occupied: no delivered worker executes.
    assert queued
    assert notification_fences and all(notification_fences)
    with conftest.TestSession() as provider:
        current = provider.get(QuotaReservation, row.id)
        assert current.dispatch_metadata["cancelled"] is True
        with pytest.raises(accounting.AccountingUnavailable, match="cancelled"):
            quota_operations._record_agent_intent(
                provider,
                row.operation_id,
                str(uuid4()),
                SimpleNamespace(id="host"),
                "/vms/create",
                {},
            )


def test_rq_cache_boundary_supports_fresh_accounting_and_confirmed_off(
    ledger, monkeypatch
):
    from app.models.host import Host
    from app.models.metering import MeteringInterval
    from app.models.provider import Provider
    from app.services import quota_observations, ws_pubsub
    from tests.test_quota_observations import SharedRedis

    db, project, user, org, amounts = ledger
    provider = Provider(name=str(uuid4()), type="ocpvirt")
    db.add(provider)
    db.flush()
    host = Host(
        provider_id=provider.id,
        host_type="shared",
        billing_mode="shared",
        metering_rates={"vcpu_hour": 0, "ram_gib_hour": 0, "disk_gib_hour": 0},
    )
    db.add(host)
    db.flush()
    project.host_id, project.provider_id, project.budget_usd = host.id, provider.id, 0
    row = compute(db, project, user, org)
    db.commit()
    producer_cache = {}
    monkeypatch.setattr(quota_observations, "_client", SharedRedis())
    monkeypatch.setattr(ws_pubsub, "notify_project", Mock())
    monkeypatch.setattr(accounting, "spend", REAL_SPEND)

    def source(timestamp, state):
        monkeypatch.setattr(ws_pubsub, "_last_states", producer_cache)
        ws_pubsub._check_and_notify_project_changes(
            project.id,
            project,
            None,
            {"vm": state},
            {},
            {},
            observed_at=timestamp,
            quota_states={"vm": state},
        )
        monkeypatch.setattr(ws_pubsub, "_last_states", {})  # A separate RQ process.

    source(NOW, "running")
    with conftest.TestSession() as worker:
        assert REAL_SPEND(worker, "project", project.id, NOW) == 0
        with pytest.raises(accounting.AccountingUnavailable, match="Fresh metering"):
            REAL_SPEND(worker, "project", project.id, NOW + timedelta(minutes=3))
    enforcement.evaluate(db, NOW)
    db.commit()
    run_id = actions(db, project, "stop_run")[0].id
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    enforcement.reconcile_worker(run_id)
    dispatch.assert_called_once_with(project.id)
    source(NOW + timedelta(seconds=10), "off")
    monkeypatch.setattr(enforcement, "_now", lambda: NOW + timedelta(seconds=10))
    enforcement.reconcile_worker(run_id)
    db.expire_all()
    assert db.get(QuotaAction, run_id).state == "completed"
    assert row.observed["cpu"] == row.observed["ram_bytes"] == 0
    assert row.observed["storage_bytes"] == 10
    original = ws_pubsub.get_cached_vm_states(project.id)
    assert row.observation_id == f"budget-off:{original['observation_id']}"
    assert original["observed_at"] == NOW + timedelta(seconds=10)
    dispatch.assert_called_once()
    # This test deliberately commits and opens independent worker sessions.
    db.query(MeteringInterval).filter_by(project_id=project.id).delete(
        synchronize_session=False
    )
    project.host_id = project.provider_id = None
    db.flush()
    db.delete(host)
    db.delete(provider)
    db.commit()
