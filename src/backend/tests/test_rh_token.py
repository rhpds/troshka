import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db


@pytest.fixture(autouse=True)
def _clean_dev_user():
    db = TestSession()
    db.query(User).filter(User.email == "local-dev@troshka").delete()
    db.commit()
    db.close()


client = TestClient(app)


def test_save_and_get_rh_offline_token():
    resp = client.put(
        "/api/v1/auth/rh-offline-token",
        json={"offline_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.test_token_value"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "saved"

    resp = client.get("/api/v1/auth/rh-offline-token")
    data = resp.json()
    assert data["has_token"] is True
    assert data["masked"].startswith("eyJhbGci")
    assert data["masked"].endswith("...")


def test_get_rh_offline_token_empty():
    resp = client.get("/api/v1/auth/rh-offline-token")
    data = resp.json()
    assert data["has_token"] is False
    assert data["masked"] == ""


def test_delete_rh_offline_token():
    client.put(
        "/api/v1/auth/rh-offline-token",
        json={"offline_token": "some_token_value"},
    )
    resp = client.delete("/api/v1/auth/rh-offline-token")
    assert resp.status_code == 204

    resp = client.get("/api/v1/auth/rh-offline-token")
    assert resp.json()["has_token"] is False


def test_save_rh_offline_token_empty_rejected():
    resp = client.put(
        "/api/v1/auth/rh-offline-token",
        json={"offline_token": "  "},
    )
    assert resp.status_code == 400
