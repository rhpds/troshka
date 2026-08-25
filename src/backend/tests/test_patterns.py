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
    create_resp = client.post(
        "/api/v1/patterns",
        json={"name": "Get Test", "topology": SAMPLE_TOPOLOGY},
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
    resp = client.get(f"/api/v1/patterns/{pattern_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Get Test"


def test_update_pattern():
    create_resp = client.post(
        "/api/v1/patterns",
        json={"name": "Update Test", "topology": SAMPLE_TOPOLOGY},
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
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
    assert data["state"] in ("draft", "deploying")
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


def test_deploy_pattern_preserves_macs():
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
    # MACs are preserved for CoreOS/ignition compatibility
    assert vm_node["data"]["nics"][0]["mac"] == "52:54:00:aa:bb:cc"


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


def test_deploy_pattern_with_showroom_overrides():
    topo = copy.deepcopy(SAMPLE_TOPOLOGY)
    topo["nodes"].append(
        {
            "id": "showroom-1",
            "type": "containerNode",
            "position": {"x": 0, "y": 200},
            "data": {
                "name": "showroom",
                "isPod": True,
                "isShowroom": True,
                "buildContent": False,
                "contentRepo": "https://github.com/old/repo.git",
                "contentRef": "v0.0.1",
                "initContainers": [
                    {
                        "name": "git-cloner",
                        "envVars": [
                            {
                                "key": "GIT_REPO_URL",
                                "value": "https://github.com/old/repo.git",
                            },
                            {"key": "GIT_REPO_REF", "value": "v0.0.1"},
                        ],
                    },
                ],
            },
        }
    )
    topo["showroom"] = {
        "enabled": True,
        "content_repo": "https://github.com/old/repo.git",
        "content_ref": "v0.0.1",
        "build_content": False,
    }
    create_resp = client.post(
        "/api/v1/patterns",
        json={"name": "Showroom Override Test", "topology": topo},
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
    resp = client.post(
        f"/api/v1/patterns/{pattern_id}/deploy",
        json={
            "name": "Showroom Override Deploy",
            "showroom": {
                "content_repo": "https://github.com/new/repo.git",
                "content_ref": "v0.0.2",
            },
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    topology = resp.json()["topology"]
    showroom = next(
        n
        for n in topology["nodes"]
        if n.get("type") == "containerNode" and n["data"].get("isShowroom")
    )
    assert showroom["data"]["contentRepo"] == "https://github.com/new/repo.git"
    assert showroom["data"]["contentRef"] == "v0.0.2"
    assert showroom["data"]["buildContent"] is True
    git_cloner = next(
        ic for ic in showroom["data"]["initContainers"] if ic["name"] == "git-cloner"
    )
    assert git_cloner["envVars"][0]["value"] == "https://github.com/new/repo.git"
    assert git_cloner["envVars"][1]["value"] == "v0.0.2"
    assert topology["showroom"]["content_repo"] == "https://github.com/new/repo.git"
    assert topology["showroom"]["content_ref"] == "v0.0.2"
    assert topology["showroom"]["build_content"] is True


def _make_other_user(email: str):
    db = TestSession()
    other = User(
        email=email,
        display_name=email,
        role="user",
        auth_source="local",
        password_hash=hash_password("pass"),
    )
    db.add(other)
    db.commit()
    db.refresh(other)
    token = create_jwt(user_id=other.id, email=other.email, role=other.role)
    db.close()
    return token


def test_list_shares_owner_sees_shared_users():
    recipient = "share-recipient@example.com"
    _make_other_user(recipient)
    create_resp = client.post(
        "/api/v1/patterns",
        json={"name": "Shareable", "topology": SAMPLE_TOPOLOGY},
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]

    # No shares yet
    resp = client.get(f"/api/v1/patterns/{pattern_id}/shares", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["shared_with"] == []

    # Share, then it appears
    share_resp = client.post(
        f"/api/v1/patterns/{pattern_id}/share",
        json={"user_email": recipient},
        headers=HEADERS,
    )
    assert share_resp.status_code == 200
    resp = client.get(f"/api/v1/patterns/{pattern_id}/shares", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["shared_with"] == [recipient]


def test_list_shares_non_owner_forbidden():
    other_token = _make_other_user("share-nonowner@example.com")
    create_resp = client.post(
        "/api/v1/patterns",
        json={"name": "Owned Pattern", "topology": SAMPLE_TOPOLOGY},
        headers=HEADERS,
    )
    pattern_id = create_resp.json()["id"]
    resp = client.get(
        f"/api/v1/patterns/{pattern_id}/shares",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


def test_list_shares_pattern_not_found():
    resp = client.get("/api/v1/patterns/nonexistent-id/shares", headers=HEADERS)
    assert resp.status_code == 404


SNO_PATTERN_TOPOLOGY = {
    "nodes": [
        {
            "id": "bastion-1",
            "type": "vmNode",
            "position": {"x": 0, "y": 0},
            "data": {
                "name": "bastion",
                "os": "rhel",
                "vcpus": 2,
                "ram": 4,
                "nics": [],
                "diskControllers": [],
            },
        },
        {
            "id": "cp0-1",
            "type": "vmNode",
            "position": {"x": 0, "y": 200},
            "data": {
                "name": "cp-0",
                "os": "rhcos",
                "vcpus": 8,
                "ram": 32,
                "nics": [],
                "diskControllers": [],
            },
        },
    ],
    "edges": [],
}


def test_deploy_sno_pattern_sets_ocp_flags():
    create_resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "SNO OCP Flags Test",
            "topology": copy.deepcopy(SNO_PATTERN_TOPOLOGY),
            "recert": True,
        },
        headers=HEADERS,
    )
    assert create_resp.status_code == 201
    pattern_id = create_resp.json()["id"]

    deploy_resp = client.post(
        f"/api/v1/patterns/{pattern_id}/deploy",
        json={"name": "SNO OCP Flags Deploy"},
        headers=HEADERS,
    )
    assert deploy_resp.status_code == 201
    cp0 = next(
        n
        for n in deploy_resp.json()["topology"]["nodes"]
        if n.get("data", {}).get("os") == "rhcos"
    )
    data = cp0["data"]
    assert data["recertEnabled"] is True
    assert data["ocpMonitor"] is True
    assert data["configureBastionBrowser"] is True
    assert deploy_resp.json()["topology"]["_deploy_recert"] is True
