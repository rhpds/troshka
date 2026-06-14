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
    email="disk-test@example.com",
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


def test_setup():
    global PROJECT_ID
    resp = client.post("/api/v1/projects", json={"name": "Disk Test"}, headers=HEADERS)
    PROJECT_ID = resp.json()["id"]


def test_create_disk():
    resp = client.post(
        f"/api/v1/projects/{PROJECT_ID}/disks",
        json={
            "name": "db-data",
            "size_gb": 500,
            "format": "raw",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["size_gb"] == 500
    assert resp.json()["attached"] is False


def test_attach_and_detach_disk():
    vm_resp = client.post(
        f"/api/v1/projects/{PROJECT_ID}/vms", json={"name": "test-vm"}, headers=HEADERS
    )
    vm_id = vm_resp.json()["id"]

    list_resp = client.get(f"/api/v1/projects/{PROJECT_ID}/disks", headers=HEADERS)
    disk_id = list_resp.json()[0]["id"]

    attach_resp = client.post(
        f"/api/v1/projects/{PROJECT_ID}/disks/{disk_id}/attach/{vm_id}", headers=HEADERS
    )
    assert attach_resp.status_code == 200
    assert attach_resp.json()["attached"] is True
    assert attach_resp.json()["vm_id"] == vm_id

    detach_resp = client.post(
        f"/api/v1/projects/{PROJECT_ID}/disks/{disk_id}/detach", headers=HEADERS
    )
    assert detach_resp.status_code == 200
    assert detach_resp.json()["attached"] is False
    assert detach_resp.json()["vm_id"] is None
