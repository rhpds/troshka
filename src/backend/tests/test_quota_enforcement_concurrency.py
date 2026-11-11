"""Budget execution races use the disposable isolated PostgreSQL fixture only."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from app.models.project import Project
from app.models.quota import QuotaAction, QuotaPolicy, QuotaReservation
from app.services import quota_accounting as accounting
from app.services import quota_enforcement as enforcement
from tests.test_quota_concurrency import NOW
from tests.test_quota_concurrency import postgres as postgres_fixture

postgres = postgres_fixture

pytestmark = pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)


def setup_run(sessions, project_id, monkeypatch):
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(accounting, "spend", lambda *args: 0)
    monkeypatch.setattr(enforcement, "_now", lambda: NOW)
    monkeypatch.setattr("app.services.ws_pubsub.get_cached_vm_states", lambda pid: None)
    with sessions.begin() as db:
        project = db.get(Project, project_id)
        project.budget_usd = 0
        project.state = "active"
        project.topology = {"nodes": [{"id": "vm", "type": "vmNode", "data": {}}]}
        org_id = (
            db.query(QuotaPolicy.scope_id).filter_by(scope="organization").first()[0]
        )
        db.add(
            QuotaReservation(
                operation_id=str(uuid4()),
                project_id=project.id,
                owner_id=project.owner_id,
                organization_id=org_id,
                requested={"cpu": 2},
                dispatch_metadata={"claim": "pending-worker", "worker_finished": False},
            )
        )
        enforcement.evaluate(db, NOW)
        return (
            db.query(QuotaAction.id)
            .filter_by(project_id=project_id, kind="stop_run")
            .scalar()
        )


def test_two_reconcilers_one_provider_stop_and_no_row_locks_during_io(
    postgres, monkeypatch
):
    sessions, (projects, _, _, _), _ = postgres
    action_id = setup_run(sessions, projects[0], monkeypatch)
    entered, release = Event(), Event()

    def dispatch(project_id):
        with sessions.begin() as observer:
            observer.query(Project).filter_by(id=project_id).with_for_update(
                nowait=True
            ).one()
            row = (
                observer.query(QuotaReservation)
                .filter_by(project_id=project_id)
                .with_for_update(nowait=True)
                .one()
            )
            assert row.dispatch_metadata["cancelled"]
        entered.set()
        assert release.wait(timeout=5)

    stop = Mock(side_effect=dispatch)
    monkeypatch.setattr(enforcement, "_dispatch", stop)

    def reconcile():
        with sessions() as db:
            enforcement.reconcile_action(db, action_id, NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(reconcile)
        assert entered.wait(timeout=5)
        second = executor.submit(reconcile)
        second.result(timeout=5)
        release.set()
        first.result(timeout=5)
    assert stop.call_count == 1
    with sessions() as db:
        assert db.get(QuotaAction, action_id).attempts == 1
        with enforcement._execution_guard(db, projects[0]) as acquired:
            assert acquired  # Explicit unlock returned a reusable pooled connection.


def test_competing_evaluations_deduplicate_causes_and_warning_intents(
    postgres, monkeypatch
):
    sessions, (projects, _, org, _), _ = postgres
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(accounting, "spend", lambda *args: 100)
    with sessions.begin() as db:
        db.query(QuotaPolicy).filter_by(
            scope="organization", scope_id=org
        ).one().spending_usd = 100
    barrier = Barrier(2)

    def evaluate():
        with sessions.begin() as db:
            barrier.wait(timeout=5)
            enforcement.evaluate(db, NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(evaluate) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    with sessions() as db:
        assert db.query(QuotaAction).filter_by(kind="warning").count() == 2
        assert db.query(QuotaAction).filter_by(kind="stop").count() == 2
        assert db.query(QuotaAction).filter_by(kind="stop_run").count() == 2


def test_lost_issued_execution_recovered_after_mutex_release(postgres, monkeypatch):
    sessions, (projects, _, _, _), _ = postgres
    action_id = setup_run(sessions, projects[0], monkeypatch)
    with sessions.begin() as db:
        run = db.get(QuotaAction, action_id)
        run.state, run.attempts = "issued", 1
        run.next_attempt_at = NOW - timedelta(seconds=1)
        run.details = {"issued_at": (NOW - timedelta(minutes=5)).isoformat()}
    dispatch = Mock()
    monkeypatch.setattr(enforcement, "_dispatch", dispatch)
    with sessions() as db:
        enforcement.reconcile_action(db, action_id, NOW)
        assert db.get(QuotaAction, action_id).attempts == 2
    dispatch.assert_called_once_with(projects[0])


def test_action_details_migration_round_trip(postgres):
    _, _, engine = postgres
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/c935e6fa0421_quota_action_execution_details.py"
    )
    spec = importlib.util.spec_from_file_location("quota_action_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        assert "details" not in {
            column["name"]
            for column in inspect(connection).get_columns("quota_actions")
        }
        migration.upgrade()
        column = next(
            column
            for column in inspect(connection).get_columns("quota_actions")
            if column["name"] == "details"
        )
        assert column["nullable"] is False
