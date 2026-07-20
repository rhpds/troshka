from unittest.mock import MagicMock, patch

from app.core.auth import hash_password
from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.project import Project
from app.models.user import User
from app.services.gc_service import (
    _recovering_hosts,
    recover_host_services,
    sync_host_capacity,
)
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


@patch("app.core.database.SessionLocal")
@patch("app.services.gc_service.repair_networks")
@patch("app.services.troshkad_client.get_all_vm_states")
@patch("app.services.troshkad_client.start_job")
@patch("app.services.troshkad_client.wait_for_job")
@patch("app.services.deploy_service._setup_bmc_via_troshkad")
@patch("app.services.deploy_service._extract_bmc_config")
def test_recover_host_services_restores_networks_and_bmc(
    mock_bmc_config,
    mock_bmc_setup,
    mock_wait,
    mock_start,
    mock_vm_states,
    mock_repair,
    mock_session_cls,
):
    host = MagicMock()
    host.id = "host-1234"
    host.agent_status = "connected"

    project = MagicMock()
    project.id = "proj-5678"
    project.state = "active"
    project.deployed_topology = {"nodes": []}
    project.topology = {"nodes": []}

    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = host
    mock_db.query.return_value.filter.return_value.all.return_value = [project]
    mock_session_cls.return_value = mock_db

    mock_repair.return_value = {"repaired": 1}
    mock_vm_states.return_value = {"troshka-proj-567-abcd1234": "running"}
    mock_start.return_value = "job-123"
    mock_wait.return_value = {"result": {"reconnected": 1}}
    mock_bmc_config.return_value = {"bmc_network": {}, "vms": []}
    mock_bmc_setup.return_value = True

    _recovering_hosts.discard("host-1234")
    recover_host_services("host-1234")

    mock_repair.assert_called_once()
    mock_bmc_setup.assert_called_once()
    assert "host-1234" not in _recovering_hosts


@patch("app.core.database.SessionLocal")
@patch("app.services.gc_service.repair_networks")
def test_recover_dedup_prevents_concurrent(mock_repair, mock_session_cls):
    _recovering_hosts.add("host-dup")
    recover_host_services("host-dup")
    mock_repair.assert_not_called()
    _recovering_hosts.discard("host-dup")


@patch("app.core.database.SessionLocal")
@patch("app.services.gc_service.repair_networks")
@patch("app.services.troshkad_client.get_all_vm_states")
@patch("app.services.troshkad_client.start_job")
@patch("app.services.troshkad_client.wait_for_job")
@patch("app.services.deploy_service._setup_bmc_via_troshkad")
@patch("app.services.deploy_service._extract_bmc_config")
def test_recover_bmc_failure_does_not_block_others(
    mock_bmc_config,
    mock_bmc_setup,
    mock_wait,
    mock_start,
    mock_vm_states,
    mock_repair,
    mock_session_cls,
):
    host = MagicMock()
    host.id = "host-fail"
    host.agent_status = "connected"

    p1 = MagicMock()
    p1.id = "proj-a"
    p1.state = "active"
    p1.deployed_topology = {"nodes": []}

    p2 = MagicMock()
    p2.id = "proj-b"
    p2.state = "active"
    p2.deployed_topology = {"nodes": []}

    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = host
    mock_db.query.return_value.filter.return_value.all.return_value = [p1, p2]
    mock_session_cls.return_value = mock_db

    mock_repair.return_value = {"repaired": 2}
    mock_vm_states.return_value = {}
    mock_bmc_config.return_value = {"bmc_network": {}, "vms": []}
    mock_bmc_setup.side_effect = [Exception("boom"), True]

    _recovering_hosts.discard("host-fail")
    recover_host_services("host-fail")

    assert mock_bmc_setup.call_count == 2
    assert "host-fail" not in _recovering_hosts
