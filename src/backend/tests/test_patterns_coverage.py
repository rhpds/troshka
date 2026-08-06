"""Tests for patterns API coverage -- sharing, progress, export, helpers."""

import copy
import datetime
import re
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.pattern import Pattern, PatternShare
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

# Create test users
_db = TestSession()
_user = User(
    email="pattern-cov-test@example.com",
    display_name="Pattern Coverage",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_user2 = User(
    email="pattern-cov-other@example.com",
    display_name="Other User",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user2)
_admin = User(
    email="pattern-cov-admin@example.com",
    display_name="Admin User",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_admin)
_db.commit()
_db.refresh(_user)
_db.refresh(_user2)
_db.refresh(_admin)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
TOKEN2 = create_jwt(user_id=_user2.id, email=_user2.email, role=_user2.role)
ADMIN_TOKEN = create_jwt(user_id=_admin.id, email=_admin.email, role=_admin.role)
USER_ID = _user.id
USER2_ID = _user2.id
ADMIN_ID = _admin.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}
HEADERS2 = {"Authorization": f"Bearer {TOKEN2}"}
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

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
        {
            "id": "xy-edge__vm-1nic-1-net-1",
            "source": "vm-1",
            "target": "net-1",
            "sourceHandle": "vm-1-nic-1-source",
            "targetHandle": "net-1-target",
        }
    ],
    "startOrder": [
        {"vmId": "vm-1", "waitForVm": None},
    ],
    "externalIps": [],
    "hiddenNodeIds": [],
}


def _create_pattern(name, headers=None, **kwargs):
    """Helper to create a pattern and return its id."""
    headers = headers or HEADERS
    payload = {"name": name, "topology": SAMPLE_TOPOLOGY, **kwargs}
    resp = client.post("/api/v1/patterns", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# 1. POST /patterns/ (create_pattern) - additional cases
# ---------------------------------------------------------------------------


def test_create_pattern_duplicate_name_409():
    _create_pattern("Dup Pattern Cov")
    resp = client.post(
        "/api/v1/patterns",
        json={"name": "Dup Pattern Cov", "topology": SAMPLE_TOPOLOGY},
        headers=HEADERS,
    )
    assert resp.status_code == 409
    assert "already have a pattern" in resp.json()["detail"]


@patch("app.core.redis.enqueue_job")
def test_create_pattern_with_source_project(mock_enqueue):
    db = TestSession()
    project = Project(
        name="Source Proj Cov",
        owner_id=USER_ID,
        state="active",
        topology=SAMPLE_TOPOLOGY,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id
    db.close()

    resp = client.post(
        "/api/v1/patterns",
        json={"name": "From Source Cov", "source_project_id": pid},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["state"] == "capturing"
    assert data["source_project_id"] == pid
    mock_enqueue.assert_called_once()


def test_create_pattern_source_project_not_found_404():
    resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "Missing Source Cov",
            "source_project_id": str(uuid.uuid4()),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 404
    assert "Source project not found" in resp.json()["detail"]


def test_create_pattern_source_project_not_deployed_400():
    db = TestSession()
    project = Project(
        name="Draft Proj Cov",
        owner_id=USER_ID,
        state="draft",
        topology=SAMPLE_TOPOLOGY,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id
    db.close()

    resp = client.post(
        "/api/v1/patterns",
        json={"name": "From Draft Cov", "source_project_id": pid},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "deployed" in resp.json()["detail"]


def test_create_pattern_missing_source_and_topology_400():
    resp = client.post(
        "/api/v1/patterns",
        json={"name": "No Source No Topo Cov"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "Provide source_project_id or topology" in resp.json()["detail"]


@patch("app.core.redis.enqueue_job")
def test_create_pattern_capture_clock_target(mock_enqueue):
    db = TestSession()
    clock = datetime.datetime(2025, 1, 15, tzinfo=datetime.UTC)
    project = Project(
        name="Clock Proj Cov",
        owner_id=USER_ID,
        state="active",
        topology=SAMPLE_TOPOLOGY,
        clock_target=clock,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id
    db.close()

    resp = client.post(
        "/api/v1/patterns",
        json={
            "name": "Clock Pattern Cov",
            "source_project_id": pid,
            "capture_clock_target": True,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    # Verify clock_target was captured on the pattern model
    db2 = TestSession()
    pattern = db2.query(Pattern).filter_by(id=resp.json()["id"]).first()
    assert pattern.clock_target is not None
    db2.close()


# ---------------------------------------------------------------------------
# 2. GET /patterns/{id}/export-template
# ---------------------------------------------------------------------------


@patch("app.services.template_loader.export_topology_to_template")
def test_export_template_success(mock_export):
    mock_export.return_value = {
        "vms": [{"name": "web", "vcpus": 2, "ram": 4096}],
        "networks": [{"name": "mgmt", "cidr": "10.0.1.0/24"}],
    }
    pid = _create_pattern("Export Template Cov")
    resp = client.get(f"/api/v1/patterns/{pid}/export-template", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/yaml; charset=utf-8"
    content = resp.text
    assert "Export Template Cov" in content
    assert "Troshka infra_template export" in content


def test_export_template_not_found_404():
    resp = client.get(
        f"/api/v1/patterns/{uuid.uuid4()}/export-template", headers=HEADERS
    )
    assert resp.status_code == 404


def test_export_template_access_denied_404():
    pid = _create_pattern("Export Denied Cov", headers=HEADERS)
    resp = client.get(f"/api/v1/patterns/{pid}/export-template", headers=HEADERS2)
    assert resp.status_code == 404


@patch("app.services.template_loader.export_topology_to_template")
def test_export_template_shared_user_can_access(mock_export):
    mock_export.return_value = {"vms": [], "networks": []}
    pid = _create_pattern("Export Shared Cov", headers=HEADERS)
    # Share with user2
    client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "pattern-cov-other@example.com"},
        headers=HEADERS,
    )
    resp = client.get(f"/api/v1/patterns/{pid}/export-template", headers=HEADERS2)
    assert resp.status_code == 200


@patch("app.services.template_loader.export_topology_to_template")
def test_export_template_public_pattern(mock_export):
    mock_export.return_value = {"vms": [], "networks": []}
    pid = _create_pattern("Export Public Cov", headers=HEADERS, visibility="public")
    resp = client.get(f"/api/v1/patterns/{pid}/export-template", headers=HEADERS2)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. GET /patterns/{id}/export
# ---------------------------------------------------------------------------


def test_export_pattern_not_found_404():
    resp = client.get(f"/api/v1/patterns/{uuid.uuid4()}/export", headers=HEADERS)
    assert resp.status_code == 404


def test_export_pattern_not_available_400():
    db = TestSession()
    pattern = Pattern(
        name="Export Not Avail Cov",
        owner_id=USER_ID,
        topology=SAMPLE_TOPOLOGY,
        state="capturing",
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)
    pid = pattern.id
    db.close()

    resp = client.get(f"/api/v1/patterns/{pid}/export", headers=HEADERS)
    assert resp.status_code == 400
    assert "available" in resp.json()["detail"]


def test_export_pattern_access_denied_404():
    pid = _create_pattern("Export Access Cov", headers=HEADERS)
    resp = client.get(f"/api/v1/patterns/{pid}/export", headers=HEADERS2)
    assert resp.status_code == 404


@patch("app.services.pattern_export.stream_pattern_export")
@patch("app.services.pattern_export.estimate_export_size", return_value=1024)
def test_export_pattern_success(mock_estimate, mock_stream):
    mock_stream.return_value = iter([b"fake-tar-data"])
    pid = _create_pattern("Export Success Cov")
    resp = client.get(f"/api/v1/patterns/{pid}/export", headers=HEADERS)
    assert resp.status_code == 200
    assert "application/x-tar" in resp.headers["content-type"]
    assert "Export_Success_Cov.tar" in resp.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# 4. POST /patterns/import
# ---------------------------------------------------------------------------


@patch("app.core.redis.enqueue_job")
def test_import_pattern_success(mock_enqueue):
    import io

    fake_tar = io.BytesIO(b"fake-tar-content")
    resp = client.post(
        "/api/v1/patterns/import",
        files={"file": ("test.tar", fake_tar, "application/x-tar")},
        params={"name": "Imported Pattern Cov"},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["state"] == "importing"
    assert data["name"] == "Imported Pattern Cov"
    assert "id" in data
    mock_enqueue.assert_called_once()


@patch("app.core.redis.enqueue_job")
def test_import_pattern_default_name(mock_enqueue):
    import io

    fake_tar = io.BytesIO(b"fake-tar-content")
    resp = client.post(
        "/api/v1/patterns/import",
        files={"file": ("test.tar", fake_tar, "application/x-tar")},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    assert resp.json()["name"] == "Importing..."


# ---------------------------------------------------------------------------
# 5. PATCH /patterns/{id} - additional cases
# ---------------------------------------------------------------------------


def test_update_pattern_not_found_404():
    resp = client.patch(
        f"/api/v1/patterns/{uuid.uuid4()}",
        json={"name": "Ghost"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_update_pattern_access_denied_403():
    pid = _create_pattern("Update Access Cov", headers=HEADERS)
    resp = client.patch(
        f"/api/v1/patterns/{pid}",
        json={"name": "Stolen"},
        headers=HEADERS2,
    )
    assert resp.status_code == 403


def test_update_pattern_tags():
    pid = _create_pattern("Update Tags Cov")
    resp = client.patch(
        f"/api/v1/patterns/{pid}",
        json={"tags": {"env": "prod", "tier": "gold"}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == {"env": "prod", "tier": "gold"}


def test_update_pattern_admin_can_edit_others():
    pid = _create_pattern("Admin Edit Cov", headers=HEADERS)
    resp = client.patch(
        f"/api/v1/patterns/{pid}",
        json={"description": "Updated by admin"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "Updated by admin"


# ---------------------------------------------------------------------------
# 6. DELETE /patterns/{id} - additional cases
# ---------------------------------------------------------------------------


def test_delete_pattern_not_found_404():
    resp = client.delete(f"/api/v1/patterns/{uuid.uuid4()}", headers=HEADERS)
    assert resp.status_code == 404


def test_delete_pattern_access_denied_403():
    pid = _create_pattern("Delete Access Cov", headers=HEADERS)
    resp = client.delete(f"/api/v1/patterns/{pid}", headers=HEADERS2)
    assert resp.status_code == 403


@patch("app.core.redis.enqueue_job")
@patch("app.services.s3_storage.delete_prefix")
@patch("app.services.s3_storage.delete_file")
@patch("app.services.pattern_service.cancel_capture")
def test_delete_capturing_pattern_calls_cancel(
    mock_cancel, mock_del_file, mock_del_prefix, mock_enqueue
):
    db = TestSession()
    pattern = Pattern(
        name="Del Capturing Cov",
        owner_id=USER_ID,
        topology=SAMPLE_TOPOLOGY,
        state="capturing",
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)
    pid = pattern.id
    db.close()

    resp = client.delete(f"/api/v1/patterns/{pid}", headers=HEADERS)
    assert resp.status_code == 204
    mock_cancel.assert_called_once()
    assert mock_cancel.call_args[0][0] == pid


# ---------------------------------------------------------------------------
# 7. POST /patterns/{id}/share
# ---------------------------------------------------------------------------


def test_share_pattern_success():
    pid = _create_pattern("Share Success Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "pattern-cov-other@example.com"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["shared_with"] == "pattern-cov-other@example.com"


def test_share_pattern_not_found_404():
    resp = client.post(
        f"/api/v1/patterns/{uuid.uuid4()}/share",
        json={"user_email": "pattern-cov-other@example.com"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_share_pattern_not_owner_403():
    pid = _create_pattern("Share Not Owner Cov", headers=HEADERS)
    resp = client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "pattern-cov-test@example.com"},
        headers=HEADERS2,
    )
    assert resp.status_code == 403
    assert "Only the owner" in resp.json()["detail"]


def test_share_pattern_target_user_not_found_404():
    pid = _create_pattern("Share No Target Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "nonexistent@example.com"},
        headers=HEADERS,
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_share_pattern_with_self_400():
    pid = _create_pattern("Share Self Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "pattern-cov-test@example.com"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "yourself" in resp.json()["detail"]


def test_share_pattern_idempotent():
    pid = _create_pattern("Share Idempotent Cov")
    payload = {"user_email": "pattern-cov-other@example.com"}
    resp1 = client.post(f"/api/v1/patterns/{pid}/share", json=payload, headers=HEADERS)
    resp2 = client.post(f"/api/v1/patterns/{pid}/share", json=payload, headers=HEADERS)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Should still be shared only once
    db = TestSession()
    count = db.query(PatternShare).filter_by(pattern_id=pid, user_id=USER2_ID).count()
    assert count == 1
    db.close()


def test_shared_user_can_get_pattern():
    pid = _create_pattern("Shared Get Cov")
    client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "pattern-cov-other@example.com"},
        headers=HEADERS,
    )
    resp = client.get(f"/api/v1/patterns/{pid}", headers=HEADERS2)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Shared Get Cov"


def test_shared_user_sees_pattern_in_list():
    pid = _create_pattern("Shared List Cov")
    client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "pattern-cov-other@example.com"},
        headers=HEADERS,
    )
    resp = client.get("/api/v1/patterns", headers=HEADERS2)
    names = [p["name"] for p in resp.json()]
    assert "Shared List Cov" in names


# ---------------------------------------------------------------------------
# 8. DELETE /patterns/{id}/share/{email}
# ---------------------------------------------------------------------------


def test_revoke_share_success():
    pid = _create_pattern("Revoke Success Cov")
    client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "pattern-cov-other@example.com"},
        headers=HEADERS,
    )
    resp = client.delete(
        f"/api/v1/patterns/{pid}/share/pattern-cov-other@example.com",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["unshared"] == "pattern-cov-other@example.com"
    # Verify user2 can no longer access
    get_resp = client.get(f"/api/v1/patterns/{pid}", headers=HEADERS2)
    assert get_resp.status_code == 404


def test_revoke_share_not_found_404():
    resp = client.delete(
        f"/api/v1/patterns/{uuid.uuid4()}/share/pattern-cov-other@example.com",
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_revoke_share_not_owner_403():
    pid = _create_pattern("Revoke Not Owner Cov", headers=HEADERS)
    resp = client.delete(
        f"/api/v1/patterns/{pid}/share/pattern-cov-test@example.com",
        headers=HEADERS2,
    )
    assert resp.status_code == 403


def test_revoke_share_target_user_not_found_404():
    pid = _create_pattern("Revoke No Target Cov")
    resp = client.delete(
        f"/api/v1/patterns/{pid}/share/ghost@example.com",
        headers=HEADERS,
    )
    assert resp.status_code == 404
    assert "User not found" in resp.json()["detail"]


def test_revoke_share_not_shared_is_noop():
    """Revoking a non-existent share should succeed silently."""
    pid = _create_pattern("Revoke Noop Cov")
    resp = client.delete(
        f"/api/v1/patterns/{pid}/share/pattern-cov-other@example.com",
        headers=HEADERS,
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 9. GET /patterns/{id}/progress
# ---------------------------------------------------------------------------


def test_progress_not_found_404():
    resp = client.get(f"/api/v1/patterns/{uuid.uuid4()}/progress", headers=HEADERS)
    assert resp.status_code == 404


def test_progress_access_denied_404():
    pid = _create_pattern("Progress Denied Cov", headers=HEADERS)
    resp = client.get(f"/api/v1/patterns/{pid}/progress", headers=HEADERS2)
    assert resp.status_code == 404


@patch("app.api.patterns.get_capture_progress")
def test_progress_in_progress(mock_progress):
    mock_progress.return_value = {
        "percent": 45,
        "current_disk": "disk-1",
        "message": "Uploading...",
    }
    pid = _create_pattern("Progress Active Cov")
    resp = client.get(f"/api/v1/patterns/{pid}/progress", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["pattern_id"] == pid
    assert data["progress"]["percent"] == 45


@patch("app.api.patterns.get_capture_progress")
def test_progress_no_data(mock_progress):
    mock_progress.return_value = None
    pid = _create_pattern("Progress None Cov")
    resp = client.get(f"/api/v1/patterns/{pid}/progress", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["progress"] is None
    assert data["state"] == "available"


def test_progress_shared_user_can_access():
    pid = _create_pattern("Progress Shared Cov")
    client.post(
        f"/api/v1/patterns/{pid}/share",
        json={"user_email": "pattern-cov-other@example.com"},
        headers=HEADERS,
    )
    resp = client.get(f"/api/v1/patterns/{pid}/progress", headers=HEADERS2)
    assert resp.status_code == 200


def test_progress_public_pattern():
    pid = _create_pattern("Progress Public Cov", visibility="public")
    resp = client.get(f"/api/v1/patterns/{pid}/progress", headers=HEADERS2)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 10. POST /patterns/{id}/deploy - additional cases
# ---------------------------------------------------------------------------


def test_deploy_duplicate_project_name_409():
    pid = _create_pattern("Deploy Dup Cov")
    # First deploy
    client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={"name": "Unique Deploy Name Cov", "auto_deploy": False},
        headers=HEADERS,
    )
    # Second deploy same name
    resp = client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={"name": "Unique Deploy Name Cov", "auto_deploy": False},
        headers=HEADERS,
    )
    assert resp.status_code == 409
    assert "already have a project" in resp.json()["detail"]


def test_deploy_with_common_password():
    topo = copy.deepcopy(SAMPLE_TOPOLOGY)
    topo["nodes"].append(
        {
            "id": "bmc-net-1",
            "type": "networkNode",
            "position": {"x": 400, "y": 0},
            "data": {
                "name": "bmc",
                "networkType": "bmc",
                "bmcPassword": "old-pass",
            },
        }
    )
    topo["nodes"][0]["data"]["cloudInit"] = True
    topo["nodes"][0]["data"]["ciCloudUserPassword"] = "old-pass"
    pid = _create_pattern("Deploy Password Cov", topology=topo)
    resp = client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={
            "name": "Password Deploy Cov",
            "common_password": "new-secret",
            "auto_deploy": False,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    nodes = resp.json()["topology"]["nodes"]
    bmc_net = [n for n in nodes if n.get("data", {}).get("networkType") == "bmc"][0]
    assert bmc_net["data"]["bmcPassword"] == "new-secret"
    vm = [n for n in nodes if n["type"] == "vmNode"][0]
    assert vm["data"]["ciCloudUserPassword"] == "new-secret"


def test_deploy_with_ssh_keys():
    topo = copy.deepcopy(SAMPLE_TOPOLOGY)
    topo["nodes"][0]["data"]["cloudInit"] = True
    topo["nodes"][0]["data"]["ciSshKeys"] = ["ssh-rsa EXISTING"]
    pid = _create_pattern("Deploy SSH Cov", topology=topo)
    resp = client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={
            "name": "SSH Deploy Cov",
            "ssh_keys": ["ssh-rsa NEWKEY"],
            "auto_deploy": False,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    vm = [n for n in resp.json()["topology"]["nodes"] if n["type"] == "vmNode"][0]
    assert "ssh-rsa EXISTING" in vm["data"]["ciSshKeys"]
    assert "ssh-rsa NEWKEY" in vm["data"]["ciSshKeys"]


@patch("app.core.redis.enqueue_job")
def test_deploy_with_auto_deploy(mock_enqueue):
    pid = _create_pattern("Deploy Auto Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={"name": "Auto Deploy Cov", "auto_deploy": True},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["state"] == "deploying"
    mock_enqueue.assert_called_once()


def test_deploy_with_guid_domain_dns():
    pid = _create_pattern("Deploy GUID Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={
            "name": "GUID Deploy Cov",
            "guid": "abc123",
            "domain": "lab.example.com",
            "auto_deploy": False,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    # Verify project has guid/domain set
    project_id = resp.json()["id"]
    db = TestSession()
    project = db.query(Project).filter_by(id=project_id).first()
    assert project.guid == "abc123"
    assert project.domain == "lab.example.com"
    db.close()


def test_deploy_pattern_not_found_404():
    resp = client.post(
        f"/api/v1/patterns/{uuid.uuid4()}/deploy",
        json={"name": "Ghost Deploy Cov", "auto_deploy": False},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_deploy_access_denied_404():
    pid = _create_pattern("Deploy Denied Cov", headers=HEADERS)
    resp = client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={"name": "Denied Deploy Cov", "auto_deploy": False},
        headers=HEADERS2,
    )
    assert resp.status_code == 404


def test_deploy_clock_target_inherited():
    db = TestSession()
    clock = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
    pattern = Pattern(
        name="Deploy Clock Cov",
        owner_id=USER_ID,
        topology=SAMPLE_TOPOLOGY,
        state="available",
        clock_target=clock,
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)
    pid = pattern.id
    db.close()

    resp = client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={"name": "Clock Deploy Cov", "auto_deploy": False},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    project_id = resp.json()["id"]
    db2 = TestSession()
    project = db2.query(Project).filter_by(id=project_id).first()
    assert project.clock_target is not None
    db2.close()


def test_deploy_with_recert():
    pid = _create_pattern("Deploy Recert Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/deploy",
        json={
            "name": "Recert Deploy Cov",
            "recert": True,
            "common_password": "recert-pass",
            "auto_deploy": False,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    topo = resp.json()["topology"]
    assert topo.get("_deploy_recert") is True
    assert topo.get("_deploy_common_password") == "recert-pass"


# ---------------------------------------------------------------------------
# 11. POST /patterns/{id}/bulk-deploy - additional cases
# ---------------------------------------------------------------------------


def test_bulk_deploy_count_over_500_400():
    pid = _create_pattern("Bulk Over 500 Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/bulk-deploy",
        json={"count": 501, "name_template": "lab-{n}"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "between 1 and 500" in resp.json()["detail"]


def test_bulk_deploy_with_guid_template():
    pid = _create_pattern("Bulk GUID Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/bulk-deploy",
        json={
            "count": 2,
            "name_template": "bulk-{n}",
            "guid_template": "guid-{n}",
            "auto_deploy": False,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["projects"]) == 2
    # Verify GUIDs were set on projects
    db = TestSession()
    for p_data in data["projects"]:
        project = db.query(Project).filter_by(id=p_data["id"]).first()
        assert project.guid is not None
        assert project.guid.startswith("guid-")
    db.close()


def test_bulk_deploy_pattern_not_found_404():
    resp = client.post(
        f"/api/v1/patterns/{uuid.uuid4()}/bulk-deploy",
        json={"count": 1, "name_template": "lab-{n}"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_bulk_deploy_access_denied_404():
    pid = _create_pattern("Bulk Denied Cov", headers=HEADERS)
    resp = client.post(
        f"/api/v1/patterns/{pid}/bulk-deploy",
        json={"count": 1, "name_template": "lab-{n}"},
        headers=HEADERS2,
    )
    assert resp.status_code == 404


def test_bulk_deploy_with_domain():
    pid = _create_pattern("Bulk Domain Cov")
    resp = client.post(
        f"/api/v1/patterns/{pid}/bulk-deploy",
        json={
            "count": 2,
            "name_template": "domain-{n}",
            "domain": "lab.example.com",
            "auto_deploy": False,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    db = TestSession()
    for p_data in resp.json()["projects"]:
        project = db.query(Project).filter_by(id=p_data["id"]).first()
        assert project.domain == "lab.example.com"
    db.close()


# ---------------------------------------------------------------------------
# 12. Helper functions (unit tests)
# ---------------------------------------------------------------------------


def test_generate_mac_valid_format():
    from app.api.patterns import _generate_mac

    mac = _generate_mac()
    assert re.match(r"^52:54:00:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$", mac)


def test_generate_mac_randomness():
    from app.api.patterns import _generate_mac

    macs = {_generate_mac() for _ in range(100)}
    # With 100 random MACs, we should get many unique values
    assert len(macs) > 50


def test_remap_node_ids():
    from app.api.patterns import _remap_node_ids

    nodes = copy.deepcopy(SAMPLE_TOPOLOGY["nodes"])
    id_map = {}
    handle_id_map = {}
    _remap_node_ids(nodes, id_map, handle_id_map)

    assert "vm-1" in id_map
    assert "net-1" in id_map
    assert nodes[0]["id"] == id_map["vm-1"]
    assert nodes[1]["id"] == id_map["net-1"]
    # NIC and disk controller IDs remapped
    assert "nic-1" in handle_id_map
    assert "dp-1" in handle_id_map
    assert nodes[0]["data"]["nics"][0]["id"] == handle_id_map["nic-1"]
    assert nodes[0]["data"]["diskControllers"][0]["id"] == handle_id_map["dp-1"]


def test_remap_edges():
    from app.api.patterns import _remap_edges

    edges = [
        {
            "id": "xy-edge__vm-1nic-1-net-1",
            "source": "vm-1",
            "target": "net-1",
            "sourceHandle": "vm-1-nic-1-source",
            "targetHandle": "net-1-target",
        }
    ]
    id_map = {"vm-1": "new-vm", "net-1": "new-net"}
    handle_id_map = {"nic-1": "nic-new"}
    _remap_edges(edges, id_map, handle_id_map)

    assert edges[0]["source"] == "new-vm"
    assert edges[0]["target"] == "new-net"
    assert "nic-new" in edges[0]["sourceHandle"]
    assert "new-vm" in edges[0]["id"]
    assert "new-net" in edges[0]["id"]


def test_remap_start_order():
    from app.api.patterns import _remap_start_order

    order = [
        {"vmId": "vm-1", "waitForVm": "vm-2", "delay": 10},
        {"vmId": "vm-2", "waitForVm": None},
    ]
    id_map = {"vm-1": "new-1", "vm-2": "new-2"}
    result = _remap_start_order(order, id_map)

    assert result[0]["vmId"] == "new-1"
    assert result[0]["waitForVm"] == "new-2"
    assert result[0]["delay"] == 10
    assert result[1]["vmId"] == "new-2"
    assert result[1]["waitForVm"] is None


def test_find_bastion_vm():
    from app.api.patterns import _find_bastion_vm

    nodes = [
        {
            "id": "vm-a",
            "type": "vmNode",
            "data": {"name": "worker", "tags": {"AnsibleGroup": "workers"}},
        },
        {
            "id": "vm-b",
            "type": "vmNode",
            "data": {"name": "bastion", "tags": {"AnsibleGroup": "bastions,showroom"}},
        },
        {"id": "net-a", "type": "networkNode", "data": {"name": "mgmt"}},
    ]
    result = _find_bastion_vm(nodes)
    assert result is not None
    assert result["id"] == "vm-b"


def test_find_bastion_vm_none():
    from app.api.patterns import _find_bastion_vm

    nodes = [
        {
            "id": "vm-a",
            "type": "vmNode",
            "data": {"name": "worker", "tags": {"AnsibleGroup": "workers"}},
        },
    ]
    assert _find_bastion_vm(nodes) is None


def test_find_cloud_init_vm():
    from app.api.patterns import _find_cloud_init_vm

    nodes = [
        {"id": "vm-a", "type": "vmNode", "data": {"name": "no-ci"}},
        {"id": "vm-b", "type": "vmNode", "data": {"name": "ci-vm", "cloudInit": True}},
    ]
    result = _find_cloud_init_vm(nodes)
    assert result is not None
    assert result["id"] == "vm-b"


def test_find_cloud_init_vm_none():
    from app.api.patterns import _find_cloud_init_vm

    nodes = [
        {"id": "vm-a", "type": "vmNode", "data": {"name": "no-ci"}},
    ]
    assert _find_cloud_init_vm(nodes) is None


def test_apply_inject_vars_bastion_first():
    from app.api.patterns import _apply_inject_vars

    nodes = [
        {
            "id": "vm-ci",
            "type": "vmNode",
            "data": {"name": "ci-vm", "cloudInit": True},
        },
        {
            "id": "vm-bastion",
            "type": "vmNode",
            "data": {
                "name": "bastion",
                "cloudInit": True,
                "tags": {"AnsibleGroup": "bastions"},
            },
        },
    ]
    _apply_inject_vars(nodes, {"guid": "xyz"})
    # Bastion should get the inject vars, not the plain cloud-init VM
    assert nodes[1]["data"].get("ciInjectVars") == {"guid": "xyz"}
    assert "ciInjectVars" not in nodes[0]["data"]


def test_apply_inject_vars_fallback_to_cloud_init():
    from app.api.patterns import _apply_inject_vars

    nodes = [
        {
            "id": "vm-ci",
            "type": "vmNode",
            "data": {"name": "ci-vm", "cloudInit": True},
        },
    ]
    _apply_inject_vars(nodes, {"guid": "xyz"})
    assert nodes[0]["data"].get("ciInjectVars") == {"guid": "xyz"}


def test_apply_inject_vars_no_target():
    from app.api.patterns import _apply_inject_vars

    nodes = [
        {"id": "vm-a", "type": "vmNode", "data": {"name": "plain"}},
    ]
    _apply_inject_vars(nodes, {"guid": "xyz"})
    # Should not crash, no inject vars set
    assert "ciInjectVars" not in nodes[0]["data"]


def test_apply_common_password():
    from app.api.patterns import _apply_common_password

    nodes = [
        {
            "id": "bmc-net",
            "type": "networkNode",
            "data": {"name": "bmc", "networkType": "bmc", "bmcPassword": "old"},
        },
        {
            "id": "vm-ci",
            "type": "vmNode",
            "data": {"name": "vm", "cloudInit": True, "ciCloudUserPassword": "old"},
        },
        {
            "id": "vm-plain",
            "type": "vmNode",
            "data": {"name": "plain"},
        },
    ]
    _apply_common_password(nodes, "new-pass")
    assert nodes[0]["data"]["bmcPassword"] == "new-pass"
    assert nodes[1]["data"]["ciCloudUserPassword"] == "new-pass"
    assert "ciCloudUserPassword" not in nodes[2]["data"]


def test_apply_ssh_keys():
    from app.api.patterns import _apply_ssh_keys

    nodes = [
        {
            "id": "vm-ci",
            "type": "vmNode",
            "data": {
                "name": "vm",
                "cloudInit": True,
                "ciSshKeys": ["ssh-rsa KEY1"],
            },
        },
        {
            "id": "vm-plain",
            "type": "vmNode",
            "data": {"name": "plain"},
        },
    ]
    _apply_ssh_keys(nodes, ["ssh-rsa KEY2", "ssh-rsa KEY1"])
    # Merged and deduplicated
    keys = nodes[0]["data"]["ciSshKeys"]
    assert "ssh-rsa KEY1" in keys
    assert "ssh-rsa KEY2" in keys
    assert len(keys) == 2
    # Plain VM unchanged
    assert "ciSshKeys" not in nodes[1]["data"]


def test_apply_ssh_keys_empty_existing():
    from app.api.patterns import _apply_ssh_keys

    nodes = [
        {
            "id": "vm-ci",
            "type": "vmNode",
            "data": {"name": "vm", "cloudInit": True},
        },
    ]
    _apply_ssh_keys(nodes, ["ssh-rsa NEWKEY"])
    assert nodes[0]["data"]["ciSshKeys"] == ["ssh-rsa NEWKEY"]


def test_remap_topology_full():
    """Full topology remap preserves structure but changes all IDs."""
    from app.api.patterns import _remap_topology

    topo = copy.deepcopy(SAMPLE_TOPOLOGY)
    topo["hiddenNodeIds"] = ["vm-1"]
    topo["externalIps"] = [{"id": "eip-1", "name": "public", "ip": "1.2.3.4"}]
    topo["nodes"][0]["data"]["portForwards"] = [{"port": 443, "extIpId": "eip-1"}]

    result = _remap_topology(topo)

    # Node IDs changed
    assert result["nodes"][0]["id"] != "vm-1"
    assert result["nodes"][1]["id"] != "net-1"
    # Edge references updated
    new_node_ids = {n["id"] for n in result["nodes"]}
    for edge in result["edges"]:
        assert edge["source"] in new_node_ids
        assert edge["target"] in new_node_ids
    # Start order remapped
    assert result["startOrder"][0]["vmId"] != "vm-1"
    assert result["startOrder"][0]["vmId"] in new_node_ids
    # Hidden node IDs remapped
    assert result["hiddenNodeIds"][0] != "vm-1"
    assert result["hiddenNodeIds"][0] in new_node_ids
    # External IPs remapped
    assert result["externalIps"][0]["id"] != "eip-1"
    assert result["externalIps"][0]["ip"] == ""  # IP cleared
    # Port forwards updated
    vm_node = [n for n in result["nodes"] if n["type"] == "vmNode"][0]
    pf = vm_node["data"]["portForwards"][0]
    assert pf["extIpId"] != "eip-1"
    assert pf["extIpId"] == result["externalIps"][0]["id"]


def test_remap_boot_devices():
    from app.api.patterns import _remap_boot_devices

    nodes = [
        {
            "id": "new-vm",
            "type": "vmNode",
            "data": {"bootDevices": ["storage-1", "network", "storage-2"]},
        }
    ]
    id_map = {"storage-1": "new-storage-1", "storage-2": "new-storage-2"}
    _remap_boot_devices(nodes, id_map)
    assert nodes[0]["data"]["bootDevices"] == [
        "new-storage-1",
        "network",
        "new-storage-2",
    ]


def test_clear_external_endpoints():
    from app.api.patterns import _clear_external_endpoints

    nodes = [
        {
            "id": "vm-1",
            "type": "vmNode",
            "data": {"externalEndpoints": [{"url": "https://old.example.com"}]},
        },
        {"id": "vm-2", "type": "vmNode", "data": {"name": "no-endpoints"}},
    ]
    _clear_external_endpoints(nodes)
    assert nodes[0]["data"]["externalEndpoints"] == []
    assert "externalEndpoints" not in nodes[1]["data"]


# ---------------------------------------------------------------------------
# List patterns - additional filter tests
# ---------------------------------------------------------------------------


def test_list_patterns_search_prefix():
    _create_pattern("SearchPrefix Alpha Cov")
    _create_pattern("SearchPrefix Beta Cov")
    resp = client.get(
        "/api/v1/patterns",
        params={"search": "SearchPrefix"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert "SearchPrefix Alpha Cov" in names
    assert "SearchPrefix Beta Cov" in names


def test_list_patterns_admin_sees_all():
    _create_pattern("Admin Sees This Cov", headers=HEADERS)
    resp = client.get("/api/v1/patterns", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert "Admin Sees This Cov" in names
