from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.main import app
from tests.conftest import TestSession, get_test_db, test_engine

Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def test_health_check():
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["app"] == "troshka"


def test_login_creates_user_and_returns_jwt():
    resp = client.post("/api/v1/auth/register", json={
        "email": "admin@example.com",
        "password": "secret123",
        "display_name": "Admin User",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "token" in data
    assert data["email"] == "admin@example.com"


def test_login_with_credentials():
    resp = client.post("/api/v1/auth/login", json={
        "email": "admin@example.com",
        "password": "secret123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data


def test_login_wrong_password():
    resp = client.post("/api/v1/auth/login", json={
        "email": "admin@example.com",
        "password": "wrongpassword",
    })
    assert resp.status_code == 401


def test_auth_me_without_token():
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_auth_me_with_token():
    login_resp = client.post("/api/v1/auth/login", json={
        "email": "admin@example.com",
        "password": "secret123",
    })
    token = login_resp.json()["token"]
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "admin@example.com"
