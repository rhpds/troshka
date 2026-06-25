import json

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(
    email="ptr-test@example.com",
    display_name="PTR Tester",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_get_includes_pull_through_fields():
    resp = client.get("/api/v1/auth/ocp-pull-secret", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["pull_through_registry"] is False
    assert data["pull_through_registry_url"] == ""


def test_put_with_pull_through_creds():
    resp = client.put(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={
            "pull_through_registry": True,
            "pull_through_registry_url": "my-registry.example.com",
            "pull_through_registry_user": "puller",
            "pull_through_registry_password": "secret123",
        },
    )
    assert resp.status_code == 200
    get_resp = client.get("/api/v1/auth/ocp-pull-secret", headers=HEADERS)
    data = get_resp.json()
    assert data["pull_through_registry"] is True
    assert data["pull_through_registry_url"] == "my-registry.example.com"
    assert data["has_secret"] is True


def test_put_pull_through_constructs_pull_secret():
    client.put(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={
            "pull_through_registry": True,
            "pull_through_registry_url": "my-registry.example.com",
            "pull_through_registry_user": "puller",
            "pull_through_registry_password": "secret123",
        },
    )
    db = TestSession()
    u = db.query(User).filter_by(email="ptr-test@example.com").first()
    from app.core.encryption import decrypt

    ps = json.loads(decrypt(u.ocp_pull_secret))
    assert "my-registry.example.com" in ps["auths"]
    db.close()


def test_put_pull_through_requires_all_fields():
    resp = client.put(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={
            "pull_through_registry": True,
            "pull_through_registry_url": "my-registry.example.com",
        },
    )
    assert resp.status_code == 400


def test_patch_toggle():
    client.put(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={
            "pull_through_registry": True,
            "pull_through_registry_url": "my-registry.example.com",
            "pull_through_registry_user": "puller",
            "pull_through_registry_password": "secret123",
        },
    )
    resp = client.patch(
        "/api/v1/auth/ocp-pull-secret",
        headers=HEADERS,
        json={"pull_through_registry": False},
    )
    assert resp.status_code == 200
    get_resp = client.get("/api/v1/auth/ocp-pull-secret", headers=HEADERS)
    assert get_resp.json()["pull_through_registry"] is False


def test_build_pull_through_config():
    from app.api.projects import _build_pull_through_config

    config = _build_pull_through_config("my-registry.example.com")
    assert config["enabled"] is True
    assert config["url"] == "my-registry.example.com"
    assert config["orgs"]["registry.redhat.io"] == "registry_redhat_io"
    assert config["orgs"]["quay.io"] == "quay_io"
