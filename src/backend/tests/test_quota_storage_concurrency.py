"""Real independent PostgreSQL sessions: physical admission and pool reservations."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, local

import pytest

from app.models.project import Project
from app.models.quota import QuotaPolicy, QuotaReservation
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, QuotaDenied, Resources
from app.services import quota_admission as admission
from app.services import quota_operations as operations
from app.services import quota_storage as storage
from tests.test_quota_concurrency import NOW
from tests.test_quota_concurrency import postgres as postgres_fixture
from tests.test_quota_storage import evidence

postgres = postgres_fixture

pytestmark = pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)


def test_two_users_compete_for_initial_physical_allowance(postgres):
    sessions, (projects, _, org, _), _ = postgres
    with sessions.begin() as db:
        db.query(QuotaPolicy).filter_by(
            scope="organization", scope_id=org
        ).one().storage_bytes = (100 * 2**20)
    barrier = Barrier(2)

    def request(index):
        with sessions() as db:
            barrier.wait(timeout=5)
            try:
                admission.reserve(
                    db,
                    projects[index],
                    f"physical-{index}",
                    Resources(storage_bytes=80 * 2**20),
                    NOW,
                )
                db.commit()
                return "admitted"
            except QuotaDenied:
                db.rollback()
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, (0, 1)))
    assert sorted(results) == ["admitted", "denied"]


def test_separate_organizations_share_one_pool_reservation_lock(postgres, monkeypatch):
    monkeypatch.setattr(operations, "now", lambda: NOW)
    sessions, (projects, users, _, destination), _ = postgres
    with sessions.begin() as db:
        db.get(User, users[1]).organization_id = destination
    barrier = Barrier(2)

    def request(index):
        with sessions() as db:
            barrier.wait(timeout=5)
            operation = f"pool-{index}"
            try:
                admission.reserve(
                    db,
                    projects[index],
                    operation,
                    Resources(storage_bytes=80 * 2**20),
                    NOW,
                )
                item = evidence(
                    db.get(Project, projects[index]),
                    generation=operation,
                    free=5 * 2**30 + 120 * 2**20,
                )
                item["host_id"] = "same-host"
                storage.check_pool(db, item, requested=80 * 2**20, exclude=operation)
                row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
                row.dispatch_metadata = {"storage_host_id": "same-host"}
                db.commit()
                return "admitted"
            except AccountingUnavailable:
                db.rollback()
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, (0, 1)))
    assert sorted(results) == ["admitted", "denied"]


def test_real_pool_lock_wait_revalidates_original_time_before_commit(
    postgres, monkeypatch
):
    sessions, (projects, _, _, _), _ = postgres
    waiting = Event()
    thread_clock = local()
    original_lock = storage._pool_lock
    monkeypatch.setattr(operations, "now", lambda: getattr(thread_clock, "now", NOW))

    def delayed_lock(db, host, pool):
        waiting.set()
        original_lock(db, host, pool)
        # Simulate the elapsed wall time after a real cross-session lock wait.
        thread_clock.now = NOW + timedelta(seconds=61)

    def request():
        with sessions() as db:
            admission.reserve(
                db,
                projects[0],
                "delayed-pool",
                Resources(storage_bytes=80 * 2**20),
                NOW,
            )
            item = evidence(db.get(Project, projects[0]), generation="delayed-pool")
            item["host_id"] = "same-host"
            with pytest.raises(AccountingUnavailable, match="stale"):
                storage.check_pool(
                    db, item, requested=80 * 2**20, exclude="delayed-pool"
                )
            db.rollback()
            assert item["observed_at"] == NOW.isoformat()

    with sessions() as holder:
        original_lock(holder, "same-host", "device-1")
        monkeypatch.setattr(storage, "_pool_lock", delayed_lock)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request)
            assert waiting.wait(timeout=5)
            holder.commit()
            future.result(timeout=10)
    with sessions() as db:
        assert (
            db.query(QuotaReservation).filter_by(operation_id="delayed-pool").count()
            == 0
        )
