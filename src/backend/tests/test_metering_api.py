"""HTTP tests for metering rates, live spend, invoices, and host overrides."""

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.project import Project
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
    user_id = user.id
    db.close()
    return user_id


def _create_project(name="meter-proj", **kwargs):
    user_id = _ensure_dev_user()
    db = TestSession()
    p = Project(
        id=str(uuid.uuid4()),
        name=name,
        state=kwargs.pop("state", "draft"),
        owner_id=user_id,
        **kwargs,
    )
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    return pid


def test_get_rates_merged_keys():
    resp = client.get("/api/v1/metering/rates")
    assert resp.status_code == 200
    data = resp.json()
    assert "aws" in data
    assert "kubevirt" in data
    assert "vcpu_hour" in data["aws"]
    assert data["kubevirt"]["vcpu_hour"] > 0


def test_put_rates_and_read_back():
    resp = client.put(
        "/api/v1/metering/rates",
        json={"rates": {"aws": {"vcpu_hour": 0.05, "disk_gib_hour": 0.001}}},
    )
    assert resp.status_code == 200
    aws = resp.json()["aws"]
    assert aws["vcpu_hour"] == 0.05
    assert aws["disk_gib_hour"] == 0.001
    assert aws["ram_gib_hour"] > 0


def test_patch_project_budget_and_live_spend():
    pid = _create_project()
    resp = client.patch(f"/api/v1/projects/{pid}", json={"budget_usd": 12.5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["budget_usd"] == 12.5
    assert body["budget_warned"] is False
    meter = client.get(f"/api/v1/projects/{pid}/metering")
    assert meter.status_code == 200
    data = meter.json()
    assert data["total_usd"] == 0.0
    assert data["budget_usd"] == 12.5
    assert data["currency"] == "USD"
    assert "by_kind" in data
    assert "line_items" in data
    assert "by_kind" in data["line_items"]
    assert data.get("running_since") is None


def test_delete_project_freezes_invoice():
    pid = _create_project(name="billed-draft")
    client.patch(f"/api/v1/projects/{pid}", json={"budget_usd": 5})
    deleted = client.delete(f"/api/v1/projects/{pid}")
    assert deleted.status_code == 200
    listed = client.get("/api/v1/metering/invoices")
    assert listed.status_code == 200
    rows = listed.json()
    match = [row for row in rows if row["project_id"] == pid]
    assert len(match) == 1
    assert match[0]["project_name"] == "billed-draft"
    detail = client.get(f"/api/v1/metering/invoices/{match[0]['id']}")
    assert detail.status_code == 200
    assert "line_items" in detail.json()


def test_host_patch_metering_rates():
    db = TestSession()
    host = Host(
        id=str(uuid.uuid4()),
        ip_address="10.0.0.9",
        state="active",
        agent_status="connected",
        total_vcpus=8,
        total_ram_mb=16384,
        used_vcpus=0,
        used_ram_mb=0,
    )
    db.add(host)
    db.commit()
    hid = host.id
    db.close()
    resp = client.patch(
        f"/api/v1/hosts/{hid}",
        json={"metering_rates": {"vcpu_hour": 0.2}},
    )
    assert resp.status_code == 200
    listed = client.get("/api/v1/hosts/")
    row = next(h for h in listed.json() if h["id"] == hid)
    assert row["metering_rates"]["vcpu_hour"] == 0.2


def test_host_patch_billing_mode():
    db = TestSession()
    host = Host(
        id=str(uuid.uuid4()),
        ip_address="10.0.0.10",
        state="active",
        agent_status="connected",
        total_vcpus=8,
        total_ram_mb=16384,
        used_vcpus=0,
        used_ram_mb=0,
    )
    db.add(host)
    db.commit()
    hid = host.id
    db.close()
    bad = client.patch(f"/api/v1/hosts/{hid}", json={"billing_mode": "nope"})
    assert bad.status_code == 400
    resp = client.patch(f"/api/v1/hosts/{hid}", json={"billing_mode": "dedicated"})
    assert resp.status_code == 200
    listed = client.get("/api/v1/hosts/")
    row = next(h for h in listed.json() if h["id"] == hid)
    assert row["billing_mode"] == "dedicated"


def test_metering_disabled_returns_404():
    with patch("app.api.metering.metering_enabled", return_value=False):
        assert client.get("/api/v1/metering/rates").status_code == 404
        pid = _create_project()
        assert client.get(f"/api/v1/projects/{pid}/metering").status_code == 404
        assert client.get("/api/v1/metering/invoices").status_code == 404
        assert client.get("/api/v1/metering/statements").status_code == 404


def test_list_statements_empty_ok():
    resp = client.get("/api/v1/metering/statements")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
