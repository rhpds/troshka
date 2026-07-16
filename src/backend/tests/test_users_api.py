import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from sqlalchemy.dialects import sqlite

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db

_db = TestSession()
_admin = _db.query(User).filter_by(email="users-admin@example.com").first()
if not _admin:
    _admin = User(
        email="users-admin@example.com",
        display_name="Users Admin",
        role="admin",
        auth_source="local",
        password_hash=hash_password("pass"),
    )
    _db.add(_admin)
    _db.commit()
    _db.refresh(_admin)
ADMIN_TOKEN = create_jwt(user_id=_admin.id, email=_admin.email, role=_admin.role)
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
ADMIN_ID = _admin.id
_db.close()

client = TestClient(app)


def test_list_users():
    resp = client.get("/api/v1/users/", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


def test_create_user():
    resp = client.post(
        "/api/v1/users/",
        json={"email": "newuser@example.com", "role": "user"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "newuser@example.com"
    assert data["role"] == "user"
    assert data["auth_source"] == "invited"


def test_create_user_operator():
    resp = client.post(
        "/api/v1/users/",
        json={
            "email": "ops@example.com",
            "display_name": "Ops Person",
            "role": "operator",
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["role"] == "operator"
    assert data["display_name"] == "Ops Person"


def test_create_duplicate_user():
    resp = client.post(
        "/api/v1/users/",
        json={"email": "newuser@example.com", "role": "user"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 409


def test_create_user_invalid_role():
    resp = client.post(
        "/api/v1/users/",
        json={"email": "bad@example.com", "role": "superadmin"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400


def test_update_user_role():
    list_resp = client.get("/api/v1/users/", headers=ADMIN_HEADERS)
    target = [u for u in list_resp.json() if u["email"] == "newuser@example.com"][0]

    resp = client.patch(
        f"/api/v1/users/{target['id']}",
        json={"role": "operator"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "operator"


def test_cannot_change_own_role():
    resp = client.patch(
        f"/api/v1/users/{ADMIN_ID}",
        json={"role": "user"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert "own role" in resp.json()["detail"].lower()


def test_cannot_delete_self():
    resp = client.delete(
        f"/api/v1/users/{ADMIN_ID}",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert "yourself" in resp.json()["detail"].lower()


def test_delete_user():
    list_resp = client.get("/api/v1/users/", headers=ADMIN_HEADERS)
    target = [u for u in list_resp.json() if u["email"] == "newuser@example.com"][0]

    resp = client.delete(
        f"/api/v1/users/{target['id']}",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 204


def test_delete_nonexistent_user():
    resp = client.delete(
        "/api/v1/users/00000000-0000-0000-0000-000000000000",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 404
