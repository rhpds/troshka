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
    email="vm-test@example.com",
    display_name="Test",
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
PROJECT_ID = None


def test_setup_project():
    global PROJECT_ID
    resp = client.post(
        "/api/v1/projects", json={"name": "VM Test Project"}, headers=HEADERS
    )
    assert resp.status_code == 201
    PROJECT_ID = resp.json()["id"]


def test_create_vm():
    resp = client.post(
        f"/api/v1/projects/{PROJECT_ID}/vms",
        json={
            "name": "web-server-01",
            "vcpus": 4,
            "ram_mb": 8192,
            "os_template": "rhel9",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "web-server-01"
    assert data["vcpus"] == 4
    assert data["state"] == "stopped"


def test_list_vms():
    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/vms", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_vm():
    list_resp = client.get(f"/api/v1/projects/{PROJECT_ID}/vms", headers=HEADERS)
    vm_id = list_resp.json()[0]["id"]
    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/vms/{vm_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "web-server-01"


def test_update_vm():
    list_resp = client.get(f"/api/v1/projects/{PROJECT_ID}/vms", headers=HEADERS)
    vm_id = list_resp.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/projects/{PROJECT_ID}/vms/{vm_id}",
        json={
            "vcpus": 8,
            "name": "web-server-updated",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["vcpus"] == 8
    assert resp.json()["name"] == "web-server-updated"


def test_delete_vm():
    create_resp = client.post(
        f"/api/v1/projects/{PROJECT_ID}/vms",
        json={"name": "to-delete"},
        headers=HEADERS,
    )
    vm_id = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/projects/{PROJECT_ID}/vms/{vm_id}", headers=HEADERS)
    assert resp.status_code == 204
