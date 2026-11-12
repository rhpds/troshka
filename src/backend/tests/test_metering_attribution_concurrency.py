"""Membership/source races use an explicitly configured disposable PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident

import pytest
from sqlalchemy import event

from app.models.host import Host
from app.models.metering import MeteringInterval, MonthlyStatement, ProjectInvoice
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import QuotaCharge
from app.services import metering_service as metering
from app.services import quota_accounting as accounting
from app.services.organization_service import _lock_transfer
from app.services.quota_locks import ScopeChanged
from tests.test_quota_concurrency import postgres as postgres_fixture

postgres = postgres_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "operation",
    ["reconcile", "close", "freeze", "finalize", "delete", "timer", "provider"],
)
def test_disabled_source_writer_serializes_with_membership(
    postgres, monkeypatch, operation
):
    sessions, (projects, users, original, destination), engine = postgres
    monkeypatch.setattr(metering, "quotas_enabled", lambda: False)
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    monkeypatch.setattr(metering, "metering_enabled", lambda: True)
    from app.core import database
    from app.services import deploy_service, project_timer

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(deploy_service, "notify_project", lambda *_: None)
    monkeypatch.setattr(project_timer, "_notify", lambda *_: None)
    snap = {
        "provider_type": "aws",
        "project_state": "active",
        "host_rates": {"vcpu_hour": 3, "ram_gib_hour": 0},
        "vms": [{"id": "vm", "vcpus": 2, "ram_gib": 0, "running": True}],
        "disks": [],
        "eips": [],
        "ceph": [],
    }
    monkeypatch.setattr(metering, "build_snapshot", lambda *_: snap)
    with sessions.begin() as db:
        provider = Provider(name="isolated", type="aws")
        db.add(provider)
        db.flush()
        host = Host(provider_id=provider.id)
        db.add(host)
        db.flush()
        project = db.get(Project, projects[0])
        project.state = "draft" if operation == "timer" else "active"
        project.host_id = host.id
        metering.reconcile_project(
            db, project, now=NOW - timedelta(hours=1), snapshot=snap
        )
    attempted, finished = Event(), Event()
    worker_ident = []

    @event.listens_for(engine, "before_cursor_execute")
    def lock_attempt(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            worker_ident
            and get_ident() == worker_ident[0]
            and "FOR UPDATE" in statement
        ):
            attempted.set()

    def write():
        worker_ident.append(get_ident())
        with sessions() as db:
            try:
                _write(db)
                db.commit()
                return "written"
            except ScopeChanged:
                db.rollback()
                return "retry"
            finally:
                finished.set()

    def _write(db):
        project = db.get(Project, projects[0])
        if operation == "delete":
            deploy_service._delete_project_record(projects[0])
        elif operation == "timer":
            project_timer._delete_project(db, project)
        elif operation == "provider":
            from app.api.providers import _cleanup_kubevirt_db_resources

            host = db.get(Host, project.host_id)
            _cleanup_kubevirt_db_resources(
                db, db.get(Provider, host.provider_id), {}, None
            )
        elif operation == "freeze":
            metering.freeze_invoice(db, project, now=NOW)
        elif operation == "finalize":
            metering.finalize_month(db, 2026, 10, now=NOW)
        else:
            snapshot = (
                {**snap, "project_state": "stopped"} if operation == "close" else snap
            )
            metering.reconcile_project(db, project, now=NOW, snapshot=snapshot)

    with sessions() as transfer, ThreadPoolExecutor(max_workers=1) as pool:
        owner, _ = _lock_transfer(transfer, users[0], destination)
        future = pool.submit(write)
        try:
            assert attempted.wait(3), "source writer bypassed identity locks"
            assert not finished.wait(
                0.2
            ), "source writer bypassed membership serialization"
            owner.organization_id = destination
            transfer.commit()
        finally:
            transfer.rollback()
        assert future.result(timeout=5) == "retry"
    with sessions() as db:
        _write(db)
        db.commit()
        if operation in {"delete", "timer", "provider"}:
            db.expire_all()
            assert db.get(Project, projects[0]) is None
            charges = db.query(QuotaCharge).filter_by(project_id=projects[0]).all()
            assert charges and {r.organization_id for r in charges} == {original}
            return
        rows = db.query(MeteringInterval).filter_by(project_id=projects[0]).all()
        assert rows and {r.quota_attribution["organization_id"] for r in rows} == {
            original
        }
        if operation == "freeze":
            assert (
                db.query(ProjectInvoice)
                .filter_by(project_id=projects[0])
                .one()
                .total_usd
                == 6
            )


def test_finalization_uses_transferred_source_boundaries_not_cached_rows(
    postgres, monkeypatch
):
    sessions, (projects, users, _original, destination), _engine = postgres
    monkeypatch.setattr(metering, "quotas_enabled", lambda: False)
    monkeypatch.setattr(metering, "metering_enabled", lambda: True)
    with sessions.begin() as db:
        provider = Provider(name="isolated", type="aws")
        db.add(provider)
        db.flush()
        host = Host(provider_id=provider.id)
        db.add(host)
        db.flush()
        project = db.get(Project, projects[0])
        project.state, project.host_id = "active", host.id
        db.add(
            MeteringInterval(
                project_id=project.id,
                kind="vcpu",
                resource_id="vm",
                qty=2,
                unit_rate=3,
                started_at=NOW - timedelta(days=2),
                quota_attribution=accounting.attribution(db, project),
            )
        )
    with sessions() as reader:
        old_source = (
            reader.query(MeteringInterval).filter_by(project_id=projects[0]).one()
        )
        with sessions.begin() as transfer:
            owner, _ = _lock_transfer(transfer, users[0], destination)
            accounting.split_attribution(
                transfer, projects[0], NOW - timedelta(days=1), destination
            )
            owner.organization_id = destination
        assert (
            old_source.ended_at is None
        )  # The caller holds a genuinely stale ORM source.
        metering.finalize_month(reader, 2026, 10, now=NOW)
        statement = reader.query(MonthlyStatement).filter_by(owner_id=users[0]).one()
        assert statement.total_usd == 288  # 48 wall-clock hours, 2 vCPU at $3/hour.
