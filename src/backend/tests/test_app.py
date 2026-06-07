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


def test_dev_mode_auto_auth():
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "local-dev@troshka"
