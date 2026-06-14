from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(email="integration-test@example.com", display_name="Integration", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

TOPOLOGY = {
    "nodes": [
        {"id": "vm-bastion", "type": "vmNode", "position": {"x": 0, "y": 0},
         "data": {"name": "bastion", "vcpus": 2, "ram": 4, "os": "rhel-10", "cloudInit": True,
                  "tags": {"AnsibleGroup": "bastions,showroom"},
                  "nics": [{"id": "nic-1", "name": "eth0", "mac": "52:54:00:aa:bb:cc", "model": "virtio"}],
                  "diskControllers": [{"id": "dp-1", "name": "disk0", "bus": "virtio"}]}},
        {"id": "vm-cp0", "type": "vmNode", "position": {"x": 400, "y": 0},
         "data": {"name": "cp-0", "vcpus": 4, "ram": 16, "os": "rhcos",
                  "tags": {"AnsibleGroup": "controllers"},
                  "nics": [{"id": "nic-2", "name": "eth0", "mac": "52:54:00:dd:ee:ff", "model": "virtio"}],
                  "diskControllers": [{"id": "dp-2", "name": "disk0", "bus": "virtio"}]}},
        {"id": "net-1", "type": "networkNode", "position": {"x": 200, "y": 200},
         "data": {"name": "cluster", "cidr": "10.0.0.0/24"}},
    ],
    "edges": [
        {"id": "e1", "source": "vm-bastion", "target": "net-1"},
        {"id": "e2", "source": "vm-cp0", "target": "net-1"},
    ],
}


def test_full_agnosticd_flow():
    # 1. Create pattern with tags
    create_resp = client.post("/api/v1/patterns", json={
        "name": "Integration Test Pattern",
        "topology": TOPOLOGY,
        "visibility": "public",
    }, headers=HEADERS)
    assert create_resp.status_code == 201
    pattern_id = create_resp.json()["id"]

    # 2. Look up pattern by name
    lookup_resp = client.get("/api/v1/patterns", params={"name": "Integration Test Pattern"}, headers=HEADERS)
    assert lookup_resp.status_code == 200
    assert len(lookup_resp.json()) == 1
    assert lookup_resp.json()[0]["id"] == pattern_id

    # 3. Deploy with inject_vars
    deploy_resp = client.post(f"/api/v1/patterns/{pattern_id}/deploy", json={
        "name": "Student Lab abc123",
        "inject_vars": {"guid": "abc123", "student_password": "hunter2"},
    }, headers=HEADERS)
    assert deploy_resp.status_code == 201
    project_id = deploy_resp.json()["id"]
    project_topo = deploy_resp.json()["topology"]

    # Verify tags preserved and inject_vars applied
    bastion = [n for n in project_topo["nodes"]
               if n["type"] == "vmNode" and "bastions" in n["data"].get("tags", {}).get("AnsibleGroup", "")][0]
    assert bastion["data"]["ciInjectVars"]["guid"] == "abc123"

    controllers = [n for n in project_topo["nodes"]
                   if n["type"] == "vmNode" and "controllers" in n["data"].get("tags", {}).get("AnsibleGroup", "")]
    assert len(controllers) == 1

    # 4. Create portal token
    token_resp = client.post(f"/api/v1/projects/{project_id}/portal-token", json={
        "access_level": "console",
    }, headers=HEADERS)
    assert token_resp.status_code == 201
    portal_token = token_resp.json()["token"]
    assert "portal_url" in token_resp.json()

    # 5. Access portal (no auth)
    portal_resp = client.get(f"/api/v1/portal/{portal_token}")
    assert portal_resp.status_code == 200
    assert portal_resp.json()["project_id"] == project_id
    assert portal_resp.json()["access_level"] == "console"

    # 6. Delete project → token invalidated
    client.delete(f"/api/v1/projects/{project_id}", headers=HEADERS)
    portal_resp2 = client.get(f"/api/v1/portal/{portal_token}")
    assert portal_resp2.status_code == 404
