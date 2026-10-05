from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.core.auth import hash_password
from app.models.disk import Disk
from app.models.host import Host
from app.models.metering import MeteringInterval, MeteringRateDefault, ProjectInvoice
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from app.services.metering_service import freeze_invoice, reconcile_project
from tests.conftest import TestSession

_db = TestSession()
_user = User(
    email="metering-svc@example.com",
    display_name="Meter",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
USER_ID = _user.id
_db.close()


def _setup(rates=None, running=True, budget=None):
    db = TestSession()
    provider = Provider(name=f"prov-{datetime.now(UTC).timestamp()}", type="aws")
    db.add(provider)
    db.flush()
    host = Host(
        provider_id=provider.id,
        state="running",
        metering_rates=rates
        or {
            "vcpu_hour": 1.0,
            "ram_gib_hour": 0.0,
            "disk_gib_hour": 1.0,
            "eip_hour": 0.0,
            "ceph_gib_hour": 0.0,
        },
    )
    db.add(host)
    db.flush()
    project = Project(
        name="meter-proj",
        owner_id=USER_ID,
        host_id=host.id,
        provider_id=provider.id,
        state="active",
        budget_usd=budget,
        topology={
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "vcpus": 2,
                        "ram": 4,
                        "running": running,
                        "state": "running" if running else "stopped",
                    },
                }
            ],
            "edges": [],
        },
    )
    db.add(project)
    db.flush()
    disk = Disk(project_id=project.id, name="d1", size_gb=10)
    db.add(disk)
    db.commit()
    db.refresh(project)
    db.refresh(host)
    return db, project, host


def test_running_vm_opens_cpu_and_disk():
    db, project, _host = _setup(running=True)
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    reconcile_project(db, project, now=now)
    db.commit()
    kinds = {
        row.kind for row in db.query(MeteringInterval).all() if row.ended_at is None
    }
    assert "vcpu" in kinds
    assert "ram" in kinds
    assert "disk" in kinds
    db.close()


def test_stop_closes_cpu_keeps_disk():
    db, project, host = _setup(running=True)
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    running = {
        "provider_type": "aws",
        "host_id": host.id,
        "host_rates": host.metering_rates,
        "vms": [
            {
                "id": "vm1",
                "running": True,
                "vcpus": 2,
                "ram_gib": 4,
                "host_id": host.id,
            }
        ],
        "disks": [{"id": "d1", "size_gib": 10, "host_id": host.id}],
        "eips": [],
        "ceph": [],
    }
    stopped = {**running, "vms": [{**running["vms"][0], "running": False}]}
    reconcile_project(db, project, now=t0, snapshot=running)
    db.commit()
    t1 = t0 + timedelta(hours=1)
    reconcile_project(db, project, now=t1, snapshot=stopped)
    db.commit()
    rows = db.query(MeteringInterval).filter_by(project_id=project.id).all()
    vcpu = [row for row in rows if row.kind == "vcpu"][0]
    disk = [row for row in rows if row.kind == "disk"][0]
    assert vcpu.ended_at.replace(tzinfo=UTC) == t1
    assert disk.ended_at is None
    db.close()


def test_rate_change_does_not_rewrite_closed():
    db, project, host = _setup(running=True)
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    running = {
        "provider_type": "aws",
        "host_id": host.id,
        "host_rates": dict(host.metering_rates),
        "vms": [
            {
                "id": "vm1",
                "running": True,
                "vcpus": 2,
                "ram_gib": 4,
                "host_id": host.id,
            }
        ],
        "disks": [],
        "eips": [],
        "ceph": [],
    }
    stopped = {**running, "vms": [{**running["vms"][0], "running": False}]}
    reconcile_project(db, project, now=t0, snapshot=running)
    db.commit()
    t1 = t0 + timedelta(hours=1)
    reconcile_project(db, project, now=t1, snapshot=stopped)
    db.commit()
    closed_rate = float(
        db.query(MeteringInterval)
        .filter_by(project_id=project.id, kind="vcpu")
        .one()
        .unit_rate
    )
    running["host_rates"] = {**host.metering_rates, "vcpu_hour": 9.0}
    t2 = t1 + timedelta(minutes=1)
    reconcile_project(db, project, now=t2, snapshot=running)
    db.commit()
    vcpus = (
        db.query(MeteringInterval)
        .filter_by(project_id=project.id, kind="vcpu")
        .order_by(MeteringInterval.started_at)
        .all()
    )
    assert len(vcpus) == 2
    assert float(vcpus[0].unit_rate) == closed_rate
    assert float(vcpus[1].unit_rate) == 9.0
    db.close()


@patch("app.services.metering_service._notify_budget")
@patch("app.services.project_timer._spawn_stop")
def test_budget_warn_and_stop_once(mock_stop, _notify):
    db, project, _host = _setup(
        running=True,
        budget=0.5,
        rates={
            "vcpu_hour": 10.0,
            "ram_gib_hour": 0,
            "disk_gib_hour": 0,
            "eip_hour": 0,
            "ceph_gib_hour": 0,
        },
    )
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    reconcile_project(db, project, now=t0)
    db.commit()
    t1 = t0 + timedelta(hours=1)
    spend = reconcile_project(db, project, now=t1)
    db.commit()
    db.refresh(project)
    assert spend >= 0.5
    assert project.budget_warned is True
    assert project.budget_stopped is True
    mock_stop.assert_called_once()
    spend2 = reconcile_project(db, project, now=t1 + timedelta(hours=1))
    db.commit()
    db.refresh(project)
    assert spend2 >= spend
    mock_stop.assert_called_once()
    db.close()


def test_freeze_invoice_survives_project_delete():
    db, project, _host = _setup(running=True)
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    reconcile_project(db, project, now=t0)
    db.commit()
    inv = freeze_invoice(db, project, now=t0 + timedelta(hours=2))
    db.commit()
    iid = inv.id
    pid = project.id
    db.query(MeteringInterval).filter_by(project_id=pid).delete()
    db.delete(project)
    db.commit()
    kept = db.get(ProjectInvoice, iid)
    assert kept is not None
    assert kept.project_id == pid
    db.close()


def test_type_default_used_without_host_override():
    db = TestSession()
    db.add(MeteringRateDefault(provider_type="aws", rates={"vcpu_hour": 0.25}))
    db.commit()
    db.close()
    db, project, host = _setup(rates=None, running=True)
    host.metering_rates = None
    db.commit()
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    # snapshot host_rates None — type default from DB
    snap = {
        "provider_type": "aws",
        "host_id": host.id,
        "host_rates": None,
        "vms": [
            {"id": "vm1", "running": True, "vcpus": 1, "ram_gib": 1, "host_id": host.id}
        ],
        "disks": [],
        "eips": [],
        "ceph": [],
    }
    reconcile_project(db, project, now=now, snapshot=snap)
    db.commit()
    row = db.query(MeteringInterval).filter_by(project_id=project.id, kind="vcpu").one()
    assert float(row.unit_rate) == 0.25
    db.close()
