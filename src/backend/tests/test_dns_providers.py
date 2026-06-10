import os
os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from sqlalchemy.dialects import sqlite
sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from tests.conftest import TestSession


def test_create_dns_provider():
    from app.models.dns_provider import DnsProvider

    db = TestSession()
    provider = DnsProvider(
        name="Test BIND",
        type="nsupdate",
        config={
            "server": "10.0.0.53",
            "port": 53,
            "key_name": "update-key",
            "key_secret": "secret123",
            "key_algorithm": "hmac-sha256",
            "default_zone": "example.com",
        },
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    assert provider.id is not None
    assert len(provider.id) == 36
    assert provider.name == "Test BIND"
    assert provider.type == "nsupdate"
    assert provider.config["server"] == "10.0.0.53"
    assert provider.config["default_zone"] == "example.com"
    db.delete(provider)
    db.commit()
    db.close()


from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import get_test_db

app.dependency_overrides[get_db] = get_test_db

_db2 = TestSession()
_admin = _db2.query(User).filter_by(role="admin").first()
if not _admin:
    _admin = User(email="dns-admin@example.com", display_name="Admin", role="admin",
                  auth_source="local", password_hash=hash_password("pass"))
    _db2.add(_admin)
    _db2.commit()
    _db2.refresh(_admin)
ADMIN_TOKEN = create_jwt(user_id=_admin.id, email=_admin.email, role=_admin.role)
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
_db2.close()

client = TestClient(app)


def test_create_dns_provider_api():
    resp = client.post("/api/v1/dns-providers", json={
        "name": "Test BIND API",
        "type": "nsupdate",
        "config": {
            "server": "10.0.0.53",
            "port": 53,
            "key_name": "update-key",
            "key_secret": "secret",
            "key_algorithm": "hmac-sha256",
            "default_zone": "example.com",
        },
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test BIND API"
    assert data["type"] == "nsupdate"


def test_list_dns_providers_api():
    resp = client.get("/api/v1/dns-providers", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


def test_delete_dns_provider_api():
    create_resp = client.post("/api/v1/dns-providers", json={
        "name": "To Delete DNS",
        "type": "nsupdate",
        "config": {"server": "1.2.3.4"},
    }, headers=ADMIN_HEADERS)
    pid = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/dns-providers/{pid}", headers=ADMIN_HEADERS)
    assert resp.status_code == 204
