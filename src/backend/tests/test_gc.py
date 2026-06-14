from app.core.auth import hash_password
from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.project import Project
from app.models.user import User
from app.services.gc_service import sync_host_capacity
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db

_db = TestSession()
_user = User(
    email="gc-test@example.com",
    display_name="GC Tester",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
USER_ID = _user.id

_host = Host(
    instance_id="i-test",
    instance_type="m8i.xlarge",
    state="active",
    host_type="shared",
    total_vcpus=16,
    total_ram_mb=65536,
    used_vcpus=10,
    used_ram_mb=40960,
    agent_status="connected",
    storage_size_gb=500,
)
_db.add(_host)
_db.commit()
_db.refresh(_host)
HOST_ID = _host.id

_project = Project(
    name="gc-active",
    owner_id=USER_ID,
    host_id=HOST_ID,
    state="active",
    topology={
        "nodes": [
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {"name": "vm1", "vcpus": 4, "ram": 8},
            },
            {
                "id": "vm-2",
                "type": "vmNode",
                "data": {"name": "vm2", "vcpus": 2, "ram": 4},
            },
        ],
        "edges": [],
    },
)
_db.add(_project)
_db.commit()
_db.close()


def test_sync_capacity_corrects_drift():
    db = TestSession()
    host = db.query(Host).filter_by(id=HOST_ID).first()
    assert host.used_vcpus == 10
    assert host.used_ram_mb == 40960

    result = sync_host_capacity(db, host)

    assert result["changed"] is True
    assert result["new"]["used_vcpus"] == 6
    assert result["new"]["used_ram_mb"] == 12288
    assert host.used_vcpus == 6
    assert host.used_ram_mb == 12288
    db.close()


def test_sync_capacity_no_change_when_accurate():
    db = TestSession()
    host = db.query(Host).filter_by(id=HOST_ID).first()
    result = sync_host_capacity(db, host)

    assert result["changed"] is False
    assert result["new"]["used_vcpus"] == 6
    db.close()


def test_sync_capacity_zero_when_no_projects():
    db = TestSession()
    host = Host(
        instance_id="i-empty",
        instance_type="m8i.xlarge",
        state="active",
        host_type="shared",
        total_vcpus=4,
        total_ram_mb=16384,
        used_vcpus=8,
        used_ram_mb=32768,
        agent_status="connected",
        storage_size_gb=500,
    )
    db.add(host)
    db.commit()
    db.refresh(host)

    result = sync_host_capacity(db, host)
    assert result["new"]["used_vcpus"] == 0
    assert result["new"]["used_ram_mb"] == 0
    assert result["changed"] is True

    db.delete(host)
    db.commit()
    db.close()
