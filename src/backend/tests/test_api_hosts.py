"""Tests for /api/v1/hosts endpoints to improve SonarQube coverage."""

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _ensure_dev_user():
    db = TestSession()
    user = db.query(User).filter_by(email="local-dev@troshka").first()
    if not user:
        db.close()
        client.get("/api/v1/auth/me")
        db = TestSession()
        user = db.query(User).filter_by(email="local-dev@troshka").first()
    uid = user.id
    db.close()
    return uid


def _create_host(state="active", agent_status="connected", **kwargs):
    db = TestSession()
    h = Host(
        id=str(uuid.uuid4()),
        ip_address="10.0.0.1",
        state=state,
        agent_status=agent_status,
        total_vcpus=32,
        total_ram_mb=65536,
        used_vcpus=0,
        used_ram_mb=0,
        region=kwargs.pop("region", "us-east-1"),
        **kwargs,
    )
    db.add(h)
    db.commit()
    hid = h.id
    db.close()
    return hid


def _cleanup_host(hid):
    db = TestSession()
    db.query(Host).filter_by(id=hid).delete()
    db.commit()
    db.close()


def test_expected_agent_version():
    _ensure_dev_user()
    resp = client.get("/api/v1/hosts/expected-agent-version")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert len(data["version"]) == 12


def test_overcommit():
    _ensure_dev_user()
    resp = client.get("/api/v1/hosts/overcommit")
    assert resp.status_code == 200
    data = resp.json()
    assert "cpu_ratio" in data
    assert "ram_ratio" in data


@patch("app.services.placement.sync_host_capacity")
def test_list_hosts_empty(mock_sync):
    _ensure_dev_user()
    resp = client.get("/api/v1/hosts/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@patch("app.services.placement.sync_host_capacity")
def test_list_hosts_with_host(mock_sync):
    _ensure_dev_user()
    hid = _create_host()
    try:
        resp = client.get("/api/v1/hosts/")
        assert resp.status_code == 200
        hosts = resp.json()
        assert len(hosts) >= 1
        assert any(h["id"] == hid for h in hosts)
    finally:
        _cleanup_host(hid)


@patch("app.services.placement.sync_host_capacity")
def test_list_hosts_filter_by_region(mock_sync):
    _ensure_dev_user()
    hid = _create_host(region="eu-west-1")
    try:
        resp = client.get("/api/v1/hosts/?region=eu-west-1")
        assert resp.status_code == 200
        hosts = resp.json()
        assert any(h["id"] == hid for h in hosts)

        resp2 = client.get("/api/v1/hosts/?region=ap-south-1")
        assert resp2.status_code == 200
        assert not any(h["id"] == hid for h in resp2.json())
    finally:
        _cleanup_host(hid)


def test_host_summary_empty():
    _ensure_dev_user()
    resp = client.get("/api/v1/hosts/summary")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_host_summary_with_host():
    _ensure_dev_user()
    hid = _create_host(region="us-west-2")
    try:
        resp = client.get("/api/v1/hosts/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        region_data = [r for r in data if r["region"] == "us-west-2"]
        assert len(region_data) == 1
        assert region_data[0]["total_hosts"] >= 1
    finally:
        _cleanup_host(hid)


def test_host_storage_no_connected_hosts():
    _ensure_dev_user()
    resp = client.get("/api/v1/hosts/storage")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_host_storage_with_troshkad_host():
    _ensure_dev_user()
    hid = _create_host()
    try:
        with patch(
            "app.api.hosts._get_troshkad_storage",
            return_value={"used_pct": 50, "free_gb": 100, "total_gb": 200},
        ):
            resp = client.get("/api/v1/hosts/storage")
        assert resp.status_code == 200
        data = resp.json()
        if hid in data:
            assert data[hid]["used_pct"] == 50
    finally:
        _cleanup_host(hid)


def test_add_host_provider_not_found():
    _ensure_dev_user()
    resp = client.post(
        "/api/v1/hosts/",
        json={
            "provider_id": str(uuid.uuid4()),
            "instance_type": "m5.xlarge",
        },
    )
    assert resp.status_code == 404
