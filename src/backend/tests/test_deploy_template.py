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
    email="template-test@example.com",
    display_name="Template Tester",
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


def test_deploy_template_creates_project():
    resp = client.post(
        "/api/v1/deploy-template",
        json={
            "template": "ocp-compact",
            "version": "4.16",
            "name": "My OCP Cluster",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My OCP Cluster"
    assert data["state"] == "draft"
    assert "topology" in data
    nodes = data["topology"]["nodes"]
    vm_nodes = [n for n in nodes if n["type"] == "vmNode"]
    # 3 CP + 1 bastion = 4 VMs
    assert len(vm_nodes) == 4


def test_deploy_template_rejects_unknown_overrides():
    resp = client.post(
        "/api/v1/deploy-template",
        json={
            "template": "ocp-compact",
            "version": "4.16",
            "name": "Custom OCP",
            "overrides": {"control_ram_gb": 32, "worker_count": 2},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "Unknown parameter" in resp.json()["detail"]


def test_deploy_template_rejects_invalid_template():
    resp = client.post(
        "/api/v1/deploy-template",
        json={
            "template": "nonexistent",
            "version": "4.16",
            "name": "Bad Template",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_deploy_template_rejects_invalid_version():
    resp = client.post(
        "/api/v1/deploy-template",
        json={
            "template": "ocp-compact",
            "version": "3.11",
            "name": "Bad Version",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400
