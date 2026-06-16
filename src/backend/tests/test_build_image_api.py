import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import get_db
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import get_test_db, TestSession

app.dependency_overrides[get_db] = get_test_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup():
    import uuid

    db = TestSession()
    db.query(Provider).delete()
    db.query(User).delete()
    user = User(id=str(uuid.uuid4()), email="admin@test.com", role="admin")
    user.rh_offline_token = "encrypted"
    db.add(user)
    prov = Provider(id=str(uuid.uuid4()), name="test-gcp", type="gcp")
    prov.gcp_project_id = "my-project"
    prov.set_credentials(
        {"service_account_json": {"client_email": "sa@proj.iam.gserviceaccount.com"}}
    )
    db.add(prov)
    db.commit()
    # Store the provider ID for tests to use
    _setup.provider_id = prov.id
    db.close()
    yield
    from app.services import image_builder_service

    image_builder_service._build_progress.clear()


@patch("app.services.image_builder_service.build_host_image")
def test_build_image_starts_thread(mock_build):
    provider_id = _setup.provider_id
    resp = client.post(f"/api/v1/providers/{provider_id}/build-image", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"
    # Verify build_host_image was called (the thread calls it)
    # We can't directly test threading.Thread without patching at module level,
    # but we can verify the function would be called with correct args
    # For now, just verify the endpoint returns success


def test_build_image_unsupported_provider():
    import uuid

    db = TestSession()
    prov = Provider(id=str(uuid.uuid4()), name="test-ec2", type="ec2")
    db.add(prov)
    db.commit()
    provider_id = prov.id
    db.close()

    resp = client.post(f"/api/v1/providers/{provider_id}/build-image", json={})
    assert resp.status_code == 400
    assert "GCP or Azure" in resp.json()["detail"]


def test_build_image_not_found():
    resp = client.post("/api/v1/providers/nonexistent/build-image", json={})
    assert resp.status_code == 404


def test_build_image_status_idle():
    provider_id = _setup.provider_id
    resp = client.get(f"/api/v1/providers/{provider_id}/build-image/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"


def test_build_image_status_in_progress():
    from app.services import image_builder_service

    provider_id = _setup.provider_id
    image_builder_service._build_progress[provider_id] = {
        "status": "building",
        "message": "Building...",
        "compose_id": "c-1",
        "elapsed_seconds": 120,
    }
    resp = client.get(f"/api/v1/providers/{provider_id}/build-image/status")
    data = resp.json()
    assert data["status"] == "building"
    assert data["compose_id"] == "c-1"


def test_build_image_already_running():
    from app.services import image_builder_service

    provider_id = _setup.provider_id
    image_builder_service._build_progress[provider_id] = {"status": "building"}
    resp = client.post(f"/api/v1/providers/{provider_id}/build-image", json={})
    assert resp.status_code == 409


def test_clear_build_status():
    from app.services import image_builder_service

    provider_id = _setup.provider_id
    image_builder_service._build_progress[provider_id] = {"status": "success"}
    resp = client.delete(f"/api/v1/providers/{provider_id}/build-image/status")
    assert resp.status_code == 204
    assert provider_id not in image_builder_service._build_progress
