from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(
    email="snap-test@example.com",
    display_name="Snap Tester",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_lib = Library(type="personal", owner_id=_user.id)
_db.add(_user)
_db.add(_lib)
_db.commit()
_db.refresh(_user)
_db.refresh(_lib)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _create_project_with_vm():
    topology = {
        "nodes": [
            {
                "id": "vm-1",
                "type": "vmNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "name": "webserver",
                    "vcpus": 4,
                    "ram": 8192,
                    "os": "rhel10",
                    "nics": [
                        {
                            "id": "nic-1",
                            "name": "eth0",
                            "mac": "52:54:00:aa:bb:cc",
                            "model": "virtio",
                        }
                    ],
                    "diskControllers": [
                        {"id": "dp-1", "name": "disk0", "bus": "virtio"}
                    ],
                },
            },
            {
                "id": "disk-1",
                "type": "storageNode",
                "position": {"x": 100, "y": 100},
                "data": {"name": "root", "size": 40, "format": "qcow2"},
            },
        ],
        "edges": [
            {"source": "vm-1", "target": "disk-1"},
        ],
    }
    import uuid as _uuid

    resp = client.post(
        "/api/v1/projects",
        json={
            "name": f"Snap Project {_uuid.uuid4().hex[:6]}",
            "description": "For snapshot testing",
        },
        headers=HEADERS,
    )
    assert (
        resp.status_code == 201
    ), f"Project create failed: {resp.status_code} {resp.json()}"
    project_id = resp.json()["id"]
    client.patch(
        f"/api/v1/projects/{project_id}",
        json={
            "topology": topology,
        },
        headers=HEADERS,
    )
    return project_id


def test_snapshot_vm():
    project_id = _create_project_with_vm()
    resp = client.post(
        f"/api/v1/projects/{project_id}/vms/vm-1/snapshot",
        json={
            "name": "webserver snapshot",
            "description": "Pre-configured web server",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "webserver snapshot"
    assert data["type"] == "snapshot"
    assert data["vm_config"]["vcpus"] == 4
    assert data["vm_config"]["ram"] == 8192


def test_snapshot_vm_not_found():
    project_id = _create_project_with_vm()
    resp = client.post(
        f"/api/v1/projects/{project_id}/vms/nonexistent/snapshot",
        json={
            "name": "nope",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_import_vm_snapshot():
    project_id = _create_project_with_vm()
    snap_resp = client.post(
        f"/api/v1/projects/{project_id}/vms/vm-1/snapshot",
        json={
            "name": "import test snap",
        },
        headers=HEADERS,
    )
    snapshot_id = snap_resp.json()["id"]

    new_project_resp = client.post(
        "/api/v1/projects",
        json={
            "name": "Import Target",
        },
        headers=HEADERS,
    )
    target_id = new_project_resp.json()["id"]

    resp = client.post(
        f"/api/v1/projects/{target_id}/import-vm",
        json={
            "snapshot_id": snapshot_id,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    topology = data["topology"]
    vm_nodes = [n for n in topology["nodes"] if n["type"] == "vmNode"]
    assert len(vm_nodes) == 1
    assert vm_nodes[0]["data"]["vcpus"] == 4
    assert vm_nodes[0]["data"]["ram"] == 8192
    # New UUID should be generated
    assert vm_nodes[0]["id"] != "vm-1"


def _create_snapshot_item(vm_config):
    """Create a real snapshot via the API, then overwrite its vm_config to
    simulate a legacy/partial capture."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.library import LibraryItem

    pid = _create_project_with_vm()
    snap = client.post(
        f"/api/v1/projects/{pid}/vms/vm-1/snapshot",
        json={"name": f"cfg-snap-{pid[:8]}"},
        headers=HEADERS,
    )
    sid = snap.json()["id"]
    db = TestSession()
    item = db.query(LibraryItem).filter_by(id=sid).first()
    item.vm_config = vm_config
    flag_modified(item, "vm_config")
    db.commit()
    db.close()
    return sid


def _new_project(name):
    return client.post("/api/v1/projects", json={"name": name}, headers=HEADERS).json()[
        "id"
    ]


def test_import_empty_snapshot_is_rejected():
    """A legacy/empty snapshot (no captured vm_config) must fail fast with a
    clear error rather than silently creating a 4096 GB diskless VM."""
    target = _new_project("empty snap target")
    sid = _create_snapshot_item({})
    resp = client.post(
        f"/api/v1/projects/{target}/import-vm",
        json={"snapshot_id": sid},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "configuration" in resp.json()["detail"].lower()


def test_import_snapshot_defaults_ram_to_4_gb():
    """When a (non-empty) snapshot config omits ram, the vmNode must default to
    4 GB, not 4096 (ram is a GB field)."""
    target = _new_project("partial snap target")
    sid = _create_snapshot_item({"vcpus": 2, "disks": []})
    resp = client.post(
        f"/api/v1/projects/{target}/import-vm",
        json={"snapshot_id": sid},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    vm_nodes = [n for n in resp.json()["topology"]["nodes"] if n["type"] == "vmNode"]
    assert vm_nodes[0]["data"]["ram"] == 4
