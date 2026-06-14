import copy

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
    email="pattern-test@example.com",
    display_name="Pattern Tester",
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
                "name": "web",
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
        {
            "id": "net-1",
            "type": "networkNode",
            "position": {"x": 200, "y": 0},
            "data": {"name": "mgmt", "cidr": "10.0.1.0/24"},
        },
    ],
    "edges": [
        {"source": "vm-1", "target": "net-1"},
    ],
}


def test_create_pattern_from_payload():
    resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "Test Pattern",
            "description": "A test",
            "topology": SAMPLE_TOPOLOGY,
            "visibility": "private",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Pattern"
    assert data["owner_id"] == USER_ID
    assert data["state"] == "available"
    assert data["visibility"] == "private"


def test_list_patterns():
    resp = client.get("/api/v1/patterns", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


def test_get_pattern():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    pattern_id = list_resp.json()[0]["id"]
    resp = client.get(f"/api/v1/patterns/{pattern_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test Pattern"


def test_update_pattern():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    pattern_id = list_resp.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/patterns/{pattern_id}",
        json={
            "name": "Renamed Pattern",
            "visibility": "public",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Pattern"
    assert resp.json()["visibility"] == "public"


def test_delete_pattern():
    create_resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "To Delete",
            "topology": SAMPLE_TOPOLOGY,
        },
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/patterns/{pattern_id}", headers=HEADERS)
    assert resp.status_code == 204
    get_resp = client.get(f"/api/v1/patterns/{pattern_id}", headers=HEADERS)
    assert get_resp.status_code == 404


def test_deploy_pattern_creates_project():
    create_resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "Deploy Test",
            "topology": SAMPLE_TOPOLOGY,
        },
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
    resp = client.post(
        f"/api/v1/patterns/{pattern_id}/deploy",
        json={
            "name": "My Lab Instance",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Lab Instance"
    assert data["state"] == "draft"
    assert data["topology"] is not None
    nodes = data["topology"]["nodes"]
    assert len(nodes) == 2
    # UUIDs should be different from original
    assert nodes[0]["id"] != "vm-1"
    assert nodes[1]["id"] != "net-1"


def test_deploy_pattern_remaps_edges():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    patterns = [p for p in list_resp.json() if p["name"] == "Deploy Test"]
    pattern_id = patterns[0]["id"]
    resp = client.post(
        f"/api/v1/patterns/{pattern_id}/deploy", json={}, headers=HEADERS
    )
    data = resp.json()
    edges = data["topology"]["edges"]
    node_ids = {n["id"] for n in data["topology"]["nodes"]}
    # Edges should reference new node IDs
    for edge in edges:
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids


def test_deploy_pattern_regenerates_macs():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    patterns = [p for p in list_resp.json() if p["name"] == "Deploy Test"]
    pattern_id = patterns[0]["id"]
    resp = client.post(
        f"/api/v1/patterns/{pattern_id}/deploy",
        json={"name": "MAC Test Deploy"},
        headers=HEADERS,
    )
    data = resp.json()
    vm_node = [n for n in data["topology"]["nodes"] if n["type"] == "vmNode"][0]
    # MAC should be regenerated (not the original)
    assert vm_node["data"]["nics"][0]["mac"] != "52:54:00:aa:bb:cc"


def test_bulk_deploy_pattern():
    create_resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "Bulk Test",
            "topology": SAMPLE_TOPOLOGY,
        },
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
    resp = client.post(
        f"/api/v1/patterns/{pattern_id}/bulk-deploy",
        json={
            "count": 3,
            "name_template": "lab-{n}",
            "auto_deploy": False,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["projects"]) == 3
    names = [p["name"] for p in data["projects"]]
    assert "lab-001" in names
    assert "lab-002" in names
    assert "lab-003" in names


def test_bulk_deploy_validates_count():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    pattern_id = list_resp.json()[0]["id"]
    resp = client.post(
        f"/api/v1/patterns/{pattern_id}/bulk-deploy",
        json={
            "count": 0,
            "name_template": "lab-{n}",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_dev_mode_allows_unauthenticated():
    resp = client.get("/api/v1/patterns")
    assert resp.status_code == 200


def test_deploy_pattern_preserves_vm_tags():
    topo = copy.deepcopy(SAMPLE_TOPOLOGY)
    topo["nodes"][0]["data"]["tags"] = {"AnsibleGroup": "bastions,showroom"}
    create_resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "Tag Test",
            "topology": topo,
        },
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
    deploy_resp = client.post(
        f"/api/v1/patterns/{pattern_id}/deploy",
        json={
            "name": "Tag Deploy Test",
        },
        headers=HEADERS,
    )
    assert deploy_resp.status_code == 201
    project_id = deploy_resp.json()["id"]
    # Fetch the full project to get topology
    project_resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert project_resp.status_code == 200
    vm_node = [
        n for n in project_resp.json()["topology"]["nodes"] if n["type"] == "vmNode"
    ][0]
    assert vm_node["data"]["tags"] == {"AnsibleGroup": "bastions,showroom"}


def test_list_patterns_filter_by_name():
    client.post(
        "/api/v1/patterns",
        json={
            "name": "Unique Lookup Name",
            "topology": SAMPLE_TOPOLOGY,
        },
        headers=HEADERS,
    )
    resp = client.get(
        "/api/v1/patterns", params={"name": "Unique Lookup Name"}, headers=HEADERS
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Unique Lookup Name"


def test_list_patterns_filter_by_name_not_found():
    resp = client.get(
        "/api/v1/patterns", params={"name": "Nonexistent Pattern xyz"}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 0


def test_deploy_pattern_with_inject_vars():
    topo = copy.deepcopy(SAMPLE_TOPOLOGY)
    topo["nodes"][0]["data"]["tags"] = {"AnsibleGroup": "bastions"}
    topo["nodes"][0]["data"]["cloudInit"] = True
    create_resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "Inject Vars Test",
            "topology": topo,
        },
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
    resp = client.post(
        f"/api/v1/patterns/{pattern_id}/deploy",
        json={
            "name": "Injected Deploy",
            "inject_vars": {"guid": "abc123", "student_password": "s3cret"},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    vm_node = [n for n in resp.json()["topology"]["nodes"] if n["type"] == "vmNode"][0]
    assert vm_node["data"].get("ciInjectVars") == {
        "guid": "abc123",
        "student_password": "s3cret",
    }
