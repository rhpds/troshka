"""Tests for /api/v1/projects endpoints.

Targets ~100 uncovered new-code lines in app/api/projects.py for SonarQube coverage.
"""

import uuid

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _ensure_dev_user():
    """Ensure the dev-mode user exists and return its ID."""
    db = TestSession()
    user = db.query(User).filter_by(email="local-dev@troshka").first()
    if not user:
        # Trigger auto-auth to create the user
        db.close()
        client.get("/api/v1/auth/me")
        db = TestSession()
        user = db.query(User).filter_by(email="local-dev@troshka").first()
    user_id = user.id
    db.close()
    return user_id


def _create_project(name="test-proj", state="draft", topology=None, **kwargs):
    """Create a test project in the DB and return its ID."""
    user_id = _ensure_dev_user()
    db = TestSession()
    p = Project(
        id=str(uuid.uuid4()),
        name=name,
        state=state,
        owner_id=user_id,
        topology=topology,
        **kwargs,
    )
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    return pid


# ---------------------------------------------------------------------------
# GET /projects/ — list_projects
# ---------------------------------------------------------------------------
def test_list_projects_empty():
    resp = client.get("/api/v1/projects/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_projects_returns_created():
    pid = _create_project(name="list-test")
    resp = client.get("/api/v1/projects/")
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()]
    assert pid in ids


def test_list_projects_filter_by_guid():
    pid = _create_project(name="guid-proj", guid="test-guid-123")
    resp = client.get("/api/v1/projects/", params={"guid": "test-guid-123"})
    assert resp.status_code == 200
    data = resp.json()
    matching = [p for p in data if p["id"] == pid]
    assert len(matching) == 1
    assert matching[0]["guid"] == "test-guid-123"


def test_list_projects_guid_no_match():
    resp = client.get("/api/v1/projects/", params={"guid": "nonexistent-guid"})
    assert resp.status_code == 200
    assert resp.json() == [] or all(
        p.get("guid") != "nonexistent-guid" for p in resp.json()
    )


# ---------------------------------------------------------------------------
# POST /projects/ — create_project
# ---------------------------------------------------------------------------
def test_create_project():
    resp = client.post(
        "/api/v1/projects/",
        json={"name": f"new-proj-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"].startswith("new-proj-")
    assert data["state"] == "draft"


def test_create_project_duplicate_name():
    name = f"dup-proj-{uuid.uuid4().hex[:8]}"
    resp1 = client.post("/api/v1/projects/", json={"name": name})
    assert resp1.status_code == 201
    resp2 = client.post("/api/v1/projects/", json={"name": name})
    assert resp2.status_code == 409
    assert "already have a project" in resp2.json()["detail"]


def test_create_project_with_options():
    resp = client.post(
        "/api/v1/projects/",
        json={
            "name": f"opts-proj-{uuid.uuid4().hex[:8]}",
            "description": "test desc",
            "auto_stop_minutes": 60,
            "auto_delete_minutes": 120,
            "poweroff_mode": "sequential",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["description"] == "test desc"
    assert data["poweroff_mode"] == "sequential"


# ---------------------------------------------------------------------------
# GET /projects/{id} — get_project
# ---------------------------------------------------------------------------
def test_get_project():
    pid = _create_project(name="get-test")
    resp = client.get(f"/api/v1/projects/{pid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == pid
    assert data["name"] == "get-test"
    assert data["state"] == "draft"


def test_get_project_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{fake_id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"


# ---------------------------------------------------------------------------
# PATCH /projects/{id} — update_project
# ---------------------------------------------------------------------------
def test_update_project_name():
    pid = _create_project(name="before-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"name": "after-update"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "after-update"


def test_update_project_description():
    pid = _create_project(name="desc-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"description": "new description"},
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "new description"


def test_update_project_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.patch(f"/api/v1/projects/{fake_id}", json={"name": "x"})
    assert resp.status_code == 404


def test_update_project_topology():
    pid = _create_project(name="topo-update")
    topo = {
        "nodes": [{"id": "n1", "type": "vmNode", "data": {"name": "vm1"}}],
        "edges": [],
    }
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"topology": topo},
    )
    assert resp.status_code == 200
    assert resp.json()["topology"]["nodes"][0]["id"] == "n1"


def test_update_project_tags():
    pid = _create_project(name="tags-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"tags": {"env": "test", "team": "qa"}},
    )
    assert resp.status_code == 200
    assert resp.json()["tags"]["env"] == "test"


def test_update_project_guest_permission():
    pid = _create_project(name="guest-perm")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"guest_permission": "full_access"},
    )
    assert resp.status_code == 200
    assert resp.json()["guest_permission"] == "full_access"


def test_update_project_guid():
    pid = _create_project(name="guid-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"guid": "my-guid-abc"},
    )
    assert resp.status_code == 200
    assert resp.json()["guid"] == "my-guid-abc"


def test_update_project_auto_stop_minutes():
    pid = _create_project(name="auto-stop-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"auto_stop_minutes": 30},
    )
    assert resp.status_code == 200
    assert resp.json()["auto_stop_minutes"] == 30


def test_update_project_clear_auto_stop():
    pid = _create_project(name="auto-stop-clear")
    # Set it first
    client.patch(f"/api/v1/projects/{pid}", json={"auto_stop_minutes": 30})
    # Clear it
    resp = client.patch(f"/api/v1/projects/{pid}", json={"auto_stop_minutes": None})
    assert resp.status_code == 200
    assert resp.json()["auto_stop_minutes"] is None


def test_update_project_auto_delete_minutes():
    pid = _create_project(name="auto-del-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"auto_delete_minutes": 120},
    )
    assert resp.status_code == 200
    assert resp.json()["auto_delete_minutes"] == 120


def test_update_project_clear_auto_delete():
    pid = _create_project(name="auto-del-clear")
    client.patch(f"/api/v1/projects/{pid}", json={"auto_delete_minutes": 60})
    resp = client.patch(f"/api/v1/projects/{pid}", json={"auto_delete_minutes": None})
    assert resp.status_code == 200
    assert resp.json()["auto_delete_minutes"] is None


def test_update_project_guest_exec():
    pid = _create_project(name="guest-exec-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"guest_exec_enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["guest_exec_enabled"] is False


def test_update_project_topology_single_bastion_browser():
    """Topology with multiple bastion browsers should fail."""
    pid = _create_project(name="bastion-multi")
    topo = {
        "nodes": [
            {
                "id": "v1",
                "type": "vmNode",
                "data": {"name": "vm1", "configureBastionBrowser": True},
            },
            {
                "id": "v2",
                "type": "vmNode",
                "data": {"name": "vm2", "configureBastionBrowser": True},
            },
        ],
        "edges": [],
    }
    resp = client.patch(f"/api/v1/projects/{pid}", json={"topology": topo})
    assert resp.status_code == 400
    assert "bastion browser" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# DELETE /projects/{id} — delete_project (draft only, synchronous)
# ---------------------------------------------------------------------------
def test_delete_project_draft():
    pid = _create_project(name="to-delete")
    resp = client.delete(f"/api/v1/projects/{pid}")
    assert resp.status_code == 200
    # Verify it's gone
    resp2 = client.get(f"/api/v1/projects/{pid}")
    assert resp2.status_code == 404


def test_delete_project_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.delete(f"/api/v1/projects/{fake_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /projects/templates — list_topology_templates
# ---------------------------------------------------------------------------
def test_list_topology_templates():
    resp = client.get("/api/v1/projects/templates")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# POST /projects/auto-layout
# ---------------------------------------------------------------------------
def test_auto_layout_empty():
    resp = client.post(
        "/api/v1/projects/auto-layout",
        json={"nodes": [], "edges": []},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data


def test_auto_layout_with_nodes():
    nodes = [
        {
            "id": "n1",
            "type": "vmNode",
            "position": {"x": 0, "y": 0},
            "data": {"name": "vm1"},
        },
        {
            "id": "n2",
            "type": "networkNode",
            "position": {"x": 0, "y": 0},
            "data": {"name": "net1", "subtype": "network"},
        },
    ]
    edges = [{"id": "e1", "source": "n1", "target": "n2"}]
    resp = client.post(
        "/api/v1/projects/auto-layout",
        json={"nodes": nodes, "edges": edges},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["nodes"]) == 2


# ---------------------------------------------------------------------------
# GET /projects/{id}/deploy-progress
# ---------------------------------------------------------------------------
def test_get_deploy_progress_draft():
    pid = _create_project(name="progress-test")
    resp = client.get(f"/api/v1/projects/{pid}/deploy-progress")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "draft"


def test_get_deploy_progress_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{fake_id}/deploy-progress")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /projects/{id}/vm-states
# ---------------------------------------------------------------------------
def test_get_vm_states_no_host():
    pid = _create_project(name="vm-states-test")
    resp = client.get(f"/api/v1/projects/{pid}/vm-states")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"states": {}}


def test_get_vm_states_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{fake_id}/vm-states")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /projects/{id}/kubeconfigs
# ---------------------------------------------------------------------------
def test_list_kubeconfigs_empty():
    pid = _create_project(name="kubeconfigs-test")
    resp = client.get(f"/api/v1/projects/{pid}/kubeconfigs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_kubeconfigs_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{fake_id}/kubeconfigs")
    assert resp.status_code == 404


def test_list_kubeconfigs_with_kubeconfig_in_topology():
    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "name": "sno1",
                    "label": "SNO-1",
                    "ocpKubeconfig": "apiVersion: v1\nkind: Config\nclusters: []",
                },
            }
        ],
        "edges": [],
    }
    pid = _create_project(name="kubeconfig-topo", topology=topo)
    resp = client.get(f"/api/v1/projects/{pid}/kubeconfigs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["vm_name"] == "SNO-1"
    assert data[0]["vm_id"] == "vm1"


# ---------------------------------------------------------------------------
# GET /projects/{id}/kubeconfig
# ---------------------------------------------------------------------------
def test_get_kubeconfig_not_found_no_kc():
    pid = _create_project(name="no-kc-proj")
    resp = client.get(f"/api/v1/projects/{pid}/kubeconfig")
    assert resp.status_code == 404
    assert "Kubeconfig not found" in resp.json()["detail"]


def test_get_kubeconfig_with_data():
    kc_content = "apiVersion: v1\nkind: Config\nclusters: []\n"
    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {"name": "sno1", "label": "SNO", "ocpKubeconfig": kc_content},
            }
        ],
        "edges": [],
    }
    pid = _create_project(name="kc-download", topology=topo)
    resp = client.get(f"/api/v1/projects/{pid}/kubeconfig")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-yaml"
    assert "Config" in resp.text


def test_get_kubeconfig_with_vm_param():
    kc_content = "apiVersion: v1\nkind: Config\nclusters: []\n"
    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {"name": "sno1", "label": "sno1", "ocpKubeconfig": kc_content},
            },
            {
                "id": "vm2",
                "type": "vmNode",
                "data": {"name": "sno2", "label": "sno2"},
            },
        ],
        "edges": [],
    }
    pid = _create_project(name="kc-vm-param", topology=topo)
    resp = client.get(f"/api/v1/projects/{pid}/kubeconfig", params={"vm": "sno1"})
    assert resp.status_code == 200
    assert "Config" in resp.text
    # Non-existent VM name
    resp2 = client.get(f"/api/v1/projects/{pid}/kubeconfig", params={"vm": "nosuchvm"})
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# POST /projects/{id}/deploy — validation errors
# ---------------------------------------------------------------------------
def test_deploy_project_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/deploy")
    assert resp.status_code == 404


def test_deploy_project_wrong_state():
    pid = _create_project(name="deploy-active", state="active")
    resp = client.post(f"/api/v1/projects/{pid}/deploy")
    assert resp.status_code == 409
    assert "not draft" in resp.json()["detail"]


def test_deploy_project_no_topology():
    pid = _create_project(name="deploy-no-topo", topology=None)
    resp = client.post(f"/api/v1/projects/{pid}/deploy")
    assert resp.status_code == 400
    assert "no topology" in resp.json()["detail"]


def test_deploy_project_empty_topology():
    pid = _create_project(name="deploy-empty-topo", topology={"nodes": [], "edges": []})
    resp = client.post(f"/api/v1/projects/{pid}/deploy")
    assert resp.status_code == 400
    assert "no VMs" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /projects/{id}/stop — validation errors
# ---------------------------------------------------------------------------
def test_stop_project_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/stop")
    assert resp.status_code == 404


def test_stop_project_wrong_state():
    pid = _create_project(name="stop-draft", state="draft")
    resp = client.post(f"/api/v1/projects/{pid}/stop")
    assert resp.status_code == 409
    assert "not active" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /projects/{id}/start — validation errors
# ---------------------------------------------------------------------------
def test_start_project_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/start")
    assert resp.status_code == 404


def test_start_project_wrong_state():
    pid = _create_project(name="start-draft", state="draft")
    resp = client.post(f"/api/v1/projects/{pid}/start")
    assert resp.status_code == 409
    assert "not stopped" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /projects/{id}/extend-timer
# ---------------------------------------------------------------------------
def test_extend_timer_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{fake_id}/extend-timer",
        json={"timer": "auto_stop", "add_minutes": 30},
    )
    assert resp.status_code == 404


def test_extend_timer_invalid_type():
    pid = _create_project(name="extend-invalid")
    resp = client.post(
        f"/api/v1/projects/{pid}/extend-timer",
        json={"timer": "bogus", "add_minutes": 10},
    )
    assert resp.status_code == 400
    assert "must be" in resp.json()["detail"]


def test_extend_timer_auto_stop_not_active():
    pid = _create_project(name="extend-no-timer")
    resp = client.post(
        f"/api/v1/projects/{pid}/extend-timer",
        json={"timer": "auto_stop", "add_minutes": 30},
    )
    assert resp.status_code == 400
    assert "not active" in resp.json()["detail"]


def test_extend_timer_auto_delete_not_active():
    pid = _create_project(name="extend-no-del")
    resp = client.post(
        f"/api/v1/projects/{pid}/extend-timer",
        json={"timer": "auto_delete", "add_minutes": 30},
    )
    assert resp.status_code == 400
    assert "not active" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /projects/{id}/export-template
# ---------------------------------------------------------------------------
def test_export_template_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/export-template")
    assert resp.status_code == 404


def test_export_template_empty_topology():
    pid = _create_project(name="export-empty", topology={"nodes": [], "edges": []})
    resp = client.post(f"/api/v1/projects/{pid}/export-template")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/yaml; charset=utf-8"
    assert "export" in resp.text.lower()


def test_export_template_password_mode_none():
    topo = {
        "nodes": [
            {
                "id": "v1",
                "type": "vmNode",
                "data": {"name": "vm1", "ciCloudUserPassword": "secret123"},
            },
        ],
        "edges": [],
    }
    pid = _create_project(name="export-pw-none", topology=topo)
    resp = client.post(
        f"/api/v1/projects/{pid}/export-template",
        json={"password_mode": "none"},
    )
    assert resp.status_code == 200
    assert "omitted" in resp.text.lower()


def test_export_template_password_mode_current():
    topo = {"nodes": [], "edges": []}
    pid = _create_project(name="export-pw-current", topology=topo)
    resp = client.post(
        f"/api/v1/projects/{pid}/export-template",
        json={"password_mode": "current"},
    )
    assert resp.status_code == 200
    assert "WARNING" in resp.text


def test_export_template_with_ocp_meta():
    topo = {
        "nodes": [],
        "edges": [],
        "ocpMeta": {"clusterName": "test-ocp", "baseDomain": "lab.local"},
    }
    pid = _create_project(name="export-ocp-meta", topology=topo)
    resp = client.post(f"/api/v1/projects/{pid}/export-template")
    assert resp.status_code == 200
    assert "test-ocp" in resp.text


# ---------------------------------------------------------------------------
# POST /projects/{id}/import-template — validation errors
# ---------------------------------------------------------------------------
def test_import_template_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{fake_id}/import-template",
        json={"template_yaml": {"vms": {}, "networks": {}}},
    )
    assert resp.status_code == 404


def test_import_template_wrong_state():
    pid = _create_project(name="import-active", state="active")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-template",
        json={"template_yaml": {"vms": {}, "networks": {}}},
    )
    assert resp.status_code == 409
    assert "draft" in resp.json()["detail"]


def test_import_template_missing_yaml():
    pid = _create_project(name="import-missing-yaml")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-template",
        json={},
    )
    assert resp.status_code == 400
    assert "required" in resp.json()["detail"].lower()


def test_import_template_invalid_type():
    pid = _create_project(name="import-bad-type")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-template",
        json={"template_yaml": "not-a-dict"},
    )
    assert resp.status_code == 400
    assert "mapping" in resp.json()["detail"].lower()


def test_import_template_missing_vms():
    pid = _create_project(name="import-no-vms")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-template",
        json={"template_yaml": {"networks": {}}},
    )
    assert resp.status_code == 400
    assert "vms" in resp.json()["detail"].lower()


def test_import_template_missing_networks():
    pid = _create_project(name="import-no-nets")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-template",
        json={"template_yaml": {"vms": {}}},
    )
    assert resp.status_code == 400
    assert "networks" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /projects/from-template — validation errors
# ---------------------------------------------------------------------------
def test_from_template_no_template():
    resp = client.post("/api/v1/projects/from-template", json={})
    assert resp.status_code == 400
    assert "required" in resp.json()["detail"].lower()


def test_from_template_invalid_template_id():
    resp = client.post(
        "/api/v1/projects/from-template",
        json={"template_id": "nonexistent-template-xyz"},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /projects/{id}/reconfigure — validation errors
# ---------------------------------------------------------------------------
def test_reconfigure_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/reconfigure")
    assert resp.status_code == 404


def test_reconfigure_wrong_state():
    pid = _create_project(name="reconfig-draft", state="draft")
    resp = client.post(f"/api/v1/projects/{pid}/reconfigure")
    assert resp.status_code == 409
    assert "cannot reconfigure" in resp.json()["detail"]


def test_reconfigure_no_host():
    pid = _create_project(name="reconfig-no-host", state="active")
    resp = client.post(f"/api/v1/projects/{pid}/reconfigure")
    assert resp.status_code == 400
    assert "no active deployment" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /projects/{id}/undeploy — validation & draft path
# ---------------------------------------------------------------------------
def test_undeploy_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/undeploy")
    assert resp.status_code == 404


def test_undeploy_draft_project():
    """Undeploying a draft project (no host_id) should reset to draft."""
    pid = _create_project(name="undeploy-draft", state="draft")
    resp = client.post(f"/api/v1/projects/{pid}/undeploy")
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# POST /projects/{id}/redeploy — validation errors
# ---------------------------------------------------------------------------
def test_redeploy_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/redeploy")
    assert resp.status_code == 404


def test_redeploy_wrong_state():
    pid = _create_project(name="redeploy-draft", state="draft")
    resp = client.post(f"/api/v1/projects/{pid}/redeploy")
    assert resp.status_code == 409
    assert "cannot redeploy" in resp.json()["detail"]


def test_redeploy_no_topology():
    pid = _create_project(name="redeploy-no-topo", state="active", topology=None)
    resp = client.post(f"/api/v1/projects/{pid}/redeploy")
    assert resp.status_code == 400
    assert "no topology" in resp.json()["detail"]


def test_redeploy_no_vms():
    pid = _create_project(
        name="redeploy-empty", state="active", topology={"nodes": [], "edges": []}
    )
    resp = client.post(f"/api/v1/projects/{pid}/redeploy")
    assert resp.status_code == 400
    assert "no VMs" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /projects/{id}/vms/{vm_id}/cancel-redeploy
# ---------------------------------------------------------------------------
def test_cancel_redeploy_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/cancel-redeploy")
    assert resp.status_code == 404


def test_cancel_redeploy_success():
    pid = _create_project(name="cancel-redeploy")
    vm_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{vm_id}/cancel-redeploy")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# POST /projects/{id}/force-stop — validation errors
# ---------------------------------------------------------------------------
def test_force_stop_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/force-stop")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /projects/{id}/migrate — validation
# ---------------------------------------------------------------------------
def test_migrate_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{fake_id}/migrate",
        json={"target_host_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


def test_migrate_no_host():
    pid = _create_project(name="migrate-no-host")
    resp = client.post(
        f"/api/v1/projects/{pid}/migrate",
        json={"target_host_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /projects/{id}/import-vm — validation
# ---------------------------------------------------------------------------
def test_import_vm_not_found():
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{fake_id}/import-vm",
        json={"snapshot_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


def test_import_vm_snapshot_not_found():
    pid = _create_project(name="import-vm-no-snap")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-vm",
        json={"snapshot_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404
    assert "Snapshot not found" in resp.json()["detail"]
