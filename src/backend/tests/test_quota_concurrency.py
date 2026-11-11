"""Real two-session tests; an absent disposable URL is an explicit skip."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.managed_group import ManagedGroup
from app.models.project import Project
from app.models.quota import (
    QuotaAllocation,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
)
from app.models.user import User
from app.schemas.quota import Limits, QuotaDenied, Resources
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services.quota_locks import ScopeChanged, lock_rows
from app.services.quota_policy import get_limits, set_limits

NOW = datetime(2026, 11, 11, tzinfo=UTC)
pytestmark = pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)


@pytest.fixture
def postgres():
    engine = create_engine(
        os.environ["QUOTA_TEST_POSTGRES_URL"],
        connect_args={"options": "-c statement_timeout=8000"},
    )
    schema = "quota_concurrency_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    @event.listens_for(engine, "checkout")
    def search_path(connection, _record, _proxy):
        cursor = connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        cursor.close()

    try:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions.begin() as db:
            org = ManagedGroup(
                name="organization",
                normalized_name="organization",
                is_organization=True,
            )
            destination = ManagedGroup(
                name="destination", normalized_name="destination", is_organization=True
            )
            db.add_all([org, destination])
            db.flush()
            users = [
                User(
                    email=f"user-{i}@test.invalid", organization_id=org.id, role="admin"
                )
                for i in range(2)
            ]
            db.add_all(users)
            db.flush()
            projects = [
                Project(name=f"project-{i}", owner_id=user.id, state="draft")
                for i, user in enumerate(users)
            ]
            db.add_all(projects)
            db.add_all(
                [
                    QuotaPolicy(scope="organization", scope_id=org.id, cpu=10),
                    QuotaReadiness(
                        id="baseline",
                        ready=True,
                        baseline_version="operator-baseline-v2-allocated",
                        details={"accounting_ready": True},
                    ),
                ]
            )
            db.flush()
            ids = (
                [p.id for p in projects],
                [u.id for u in users],
                org.id,
                destination.id,
            )
        yield sessions, ids, engine
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_two_users_compete_for_shared_organization_capacity(postgres):
    sessions, (projects, _, org, _), _ = postgres
    barrier = Barrier(2)

    def request(index):
        with sessions() as db:
            barrier.wait(timeout=5)
            try:
                admission.reserve(
                    db, projects[index], f"deploy-{index}", Resources(cpu=6), NOW
                )
                db.commit()
                return "admitted"
            except QuotaDenied:
                db.rollback()
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request, index) for index in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == ["admitted", "denied"]
    with sessions() as db:
        used, reserved = admission.usage(db, "organization", org)
        assert used.cpu + reserved.cpu == 6


def test_duplicate_operation_has_one_reservation(postgres):
    sessions, (projects, _, _, _), _ = postgres
    barrier = Barrier(2)

    def request():
        with sessions.begin() as db:
            barrier.wait(timeout=5)
            return admission.reserve(
                db, projects[0], "duplicate", Resources(cpu=6), NOW
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request) for _ in range(2)]
        assert len({f.result(timeout=10) for f in futures}) == 1
    with sessions() as db:
        assert db.query(QuotaReservation).count() == 1


def test_policy_reduction_serializes_with_admission(postgres):
    sessions, (projects, users, org, _), _ = postgres
    attempting = Event()
    with sessions() as writer:
        set_limits(
            writer, writer.get(User, users[0]), "organization", org, Limits(cpu=5), NOW
        )

        def request():
            with sessions() as db:
                attempting.set()
                try:
                    admission.reserve(
                        db, projects[1], "after-reduction", Resources(cpu=6), NOW
                    )
                    db.commit()
                    return "admitted"
                except QuotaDenied:
                    db.rollback()
                    return "denied"

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request)
            assert attempting.wait(timeout=5)
            writer.commit()
            assert future.result(timeout=10) == "denied"


def test_membership_race_requires_full_transaction_retry(postgres):
    sessions, (projects, users, org, destination), engine = postgres
    resolving, transferred = Event(), Event()

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

    def request():
        with sessions() as db:
            try:
                admission.reserve(
                    db, projects[0], "membership-race", Resources(cpu=6), NOW
                )
            except ScopeChanged:
                db.rollback()
                return "retry"
            raise AssertionError("stale membership was admitted")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request)
            assert resolving.wait(timeout=5)
            with sessions.begin() as db:
                lock_rows(db, user_ids=[users[0]], organization_ids=[org, destination])
                db.get(User, users[0]).organization_id = destination
            transferred.set()
            assert future.result(timeout=10) == "retry"
        with sessions() as db:
            assert db.query(QuotaReservation).count() == 0
    finally:
        transferred.set()
        event.remove(engine, "before_cursor_execute", pause_before_user_lock)


def test_accounting_refresh_serializes_live_project_observations(postgres):
    sessions, (projects, _, _, _), _ = postgres
    with sessions() as reader:
        accounting.refresh(reader, "project", projects[0], NOW)
        with sessions() as writer:
            with pytest.raises(OperationalError) as error:
                writer.query(Project).filter_by(id=projects[0]).with_for_update(
                    nowait=True
                ).one()
            assert error.value.orig.pgcode == "55P03"


def test_confirm_and_reserve_overlap_do_not_deadlock(postgres):
    sessions, (projects, _, org, _), _ = postgres
    with sessions.begin() as db:
        admission.reserve(db, projects[0], "original", Resources(cpu=6), NOW)
    barrier = Barrier(2)

    def reconcile():
        with sessions.begin() as db:
            barrier.wait(timeout=5)
            admission.confirm(db, "original", Resources(cpu=6), "observed", NOW)

    def request():
        with sessions() as db:
            barrier.wait(timeout=5)
            try:
                admission.reserve(db, projects[1], "second", Resources(cpu=6), NOW)
            except QuotaDenied:
                db.rollback()
                return "denied"
            raise AssertionError("concurrent confirmation lost usage")

    with ThreadPoolExecutor(max_workers=2) as executor:
        confirmation, reservation = executor.submit(reconcile), executor.submit(request)
        confirmation.result(timeout=10)
        assert reservation.result(timeout=10) == "denied"
    with sessions() as db:
        assert admission.usage(db, "organization", org) == (
            Resources(cpu=6),
            Resources(),
        )


def test_dispatch_metadata_migration_round_trip(postgres):
    _, _, engine = postgres
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/b824d5ef9310_quota_dispatch_metadata.py"
    )
    spec = importlib.util.spec_from_file_location("quota_dispatch_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE quota_reservations DROP COLUMN dispatch_metadata")
        )
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert (
            connection.execute(
                text(
                    "SELECT column_default FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='quota_reservations' AND column_name='dispatch_metadata'"
                )
            ).scalar_one()
            == "'{}'::jsonb"
        )
        module.downgrade()
        assert not connection.execute(
            text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='quota_reservations' AND column_name='dispatch_metadata'"
            )
        ).first()


def test_retained_allocation_cache_cannot_hide_committed_confirmation(postgres):
    sessions, (projects, _, org, _), _ = postgres
    with sessions.begin() as db:
        db.query(QuotaPolicy).filter_by(
            scope="organization", scope_id=org
        ).one().cpu = 6
        admission.reserve(db, projects[0], "partial", Resources(cpu=6), NOW)
        admission.confirm(
            db, "partial", Resources(cpu=2), "partial-observation", NOW, complete=False
        )
    with sessions() as admitting:
        cached_allocation = (
            admitting.query(QuotaAllocation).filter_by(operation_id="partial").one()
        )
        cached_reservation = (
            admitting.query(QuotaReservation).filter_by(operation_id="partial").one()
        )
        assert (
            cached_allocation.observed["cpu"] == cached_reservation.observed["cpu"] == 2
        )
        with sessions.begin() as confirmer:
            admission.confirm(
                confirmer,
                "partial",
                Resources(cpu=6),
                "complete-observation",
                NOW + timedelta(seconds=1),
            )
        with pytest.raises(QuotaDenied) as error:
            admission.reserve(
                admitting,
                projects[1],
                "cannot-fit",
                Resources(cpu=4),
                NOW + timedelta(seconds=2),
            )
        assert error.value.used == 6
        assert cached_allocation.observed["cpu"] == 6


@pytest.mark.parametrize("operation", ["reserve", "revalidate"])
def test_retained_policy_cache_cannot_hide_committed_reduction(postgres, operation):
    sessions, (projects, users, org, _), _ = postgres
    if operation == "revalidate":
        with sessions.begin() as db:
            admission.reserve(db, projects[0], "queued", Resources(cpu=6), NOW)
    with sessions() as admitting:
        cached_policy = (
            admitting.query(QuotaPolicy)
            .filter_by(scope="organization", scope_id=org)
            .one()
        )
        assert cached_policy.cpu == 10
        with sessions.begin() as writer:
            set_limits(
                writer,
                writer.get(User, users[0]),
                "organization",
                org,
                Limits(cpu=5),
                NOW,
            )
        with pytest.raises(QuotaDenied) as error:
            if operation == "reserve":
                admission.reserve(admitting, projects[0], "new", Resources(cpu=6), NOW)
            else:
                admission.revalidate(admitting, "queued", NOW)
        assert error.value.limit == cached_policy.cpu == 5


def test_authoritative_reads_preserve_and_flush_local_writes_without_autoflush(
    postgres,
):
    sessions, (projects, _, org, _), _ = postgres
    with sessions.begin() as db:
        admission.reserve(db, projects[0], "local", Resources(cpu=6), NOW)
        admission.confirm(db, "local", Resources(cpu=2), "partial", NOW, complete=False)
    with sessions() as db:
        policy = (
            db.query(QuotaPolicy).filter_by(scope="organization", scope_id=org).one()
        )
        allocation = db.query(QuotaAllocation).filter_by(operation_id="local").one()
        reservation = db.query(QuotaReservation).filter_by(operation_id="local").one()
        with db.no_autoflush:
            policy.cpu = 7
            allocation.observed = reservation.observed = Resources(cpu=3).model_dump()
            assert get_limits(db, "organization", org).cpu == 7
            assert admission.usage(db, "organization", org) == (
                Resources(cpu=3),
                Resources(cpu=3),
            )
            assert (
                db.execute(
                    text(
                        "SELECT cpu FROM quota_policies WHERE scope='organization' AND scope_id=:id"
                    ),
                    {"id": org},
                ).scalar_one()
                == 7
            )
            assert (
                db.execute(
                    text(
                        "SELECT observed->>'cpu' FROM quota_allocations WHERE operation_id='local'"
                    )
                ).scalar_one()
                == "3"
            )
