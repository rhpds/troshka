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
    email="portal-test@example.com",
    display_name="Portal Tester",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

SAMPLE_TOPOLOGY = {
    "nodes": [
        {
            "id": "vm-1",
            "type": "vmNode",
            "position": {"x": 0, "y": 0},
            "data": {
                "name": "bastion",
                "vcpus": 2,
                "ram": 4096,
                "nics": [
                    {
                        "id": "nic-1",
                        "name": "eth0",
                        "mac": "52:54:00:aa:bb:cc",
                        "model": "virtio",
                    }
                ],
                "diskControllers": [{"id": "dp-1", "name": "disk0", "bus": "virtio"}],
            },
        },
    ],
    "edges": [],
}

_project_counter = 0


def _create_project():
    global _project_counter
    _project_counter += 1
    resp = client.post(
        "/api/v1/projects",
        json={
            "name": f"Portal Test Project {_project_counter}",
            "topology": SAMPLE_TOPOLOGY,
        },
        headers=HEADERS,
    )
    return resp.json()["id"]


def test_create_portal_token():
    project_id = _create_project()
    resp = client.post(
        f"/api/v1/projects/{project_id}/portal-token",
        json={
            "access_level": "console",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "token" in data
    assert "portal_url" in data
    assert data["access_level"] == "console"


def test_get_portal_view_unauthenticated():
    project_id = _create_project()
    token_resp = client.post(
        f"/api/v1/projects/{project_id}/portal-token",
        json={
            "access_level": "readonly",
        },
        headers=HEADERS,
    )
    token = token_resp.json()["token"]
    resp = client.get(f"/api/v1/portal/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project_id
    assert data["access_level"] == "readonly"
    assert "topology" in data


def test_get_portal_invalid_token():
    resp = client.get("/api/v1/portal/nonexistent-token-xyz")
    assert resp.status_code == 404


def test_portal_token_deleted_with_project():
    project_id = _create_project()
    token_resp = client.post(
        f"/api/v1/projects/{project_id}/portal-token",
        json={
            "access_level": "power",
        },
        headers=HEADERS,
    )
    token = token_resp.json()["token"]
    client.delete(f"/api/v1/projects/{project_id}", headers=HEADERS)
    resp = client.get(f"/api/v1/portal/{token}")
    assert resp.status_code == 404


def test_portal_vm_action_requires_power_level():
    project_id = _create_project()
    token_resp = client.post(
        f"/api/v1/projects/{project_id}/portal-token",
        json={
            "access_level": "readonly",
        },
        headers=HEADERS,
    )
    token = token_resp.json()["token"]
    resp = client.post(f"/api/v1/portal/{token}/vms/vm-1/stop")
    assert resp.status_code == 403


def test_portal_vm_action_allowed_with_power():
    project_id = _create_project()
    token_resp = client.post(
        f"/api/v1/projects/{project_id}/portal-token",
        json={
            "access_level": "power",
        },
        headers=HEADERS,
    )
    token = token_resp.json()["token"]
    resp = client.post(f"/api/v1/portal/{token}/vms/vm-1/stop")
    # Will be 400 because project isn't deployed (state is "draft"), but NOT 403
    assert resp.status_code != 403
