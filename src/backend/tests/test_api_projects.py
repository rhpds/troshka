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


# ---------------------------------------------------------------------------
# Helper: create a host + project with host_id for VM operation tests
# ---------------------------------------------------------------------------
from app.models.host import Host


def _create_host(**kwargs):
    """Create a Host record in the DB and return its ID."""
    db = TestSession()
    defaults = {
        "id": str(uuid.uuid4()),
        "state": "active",
        "agent_status": "connected",
        "ip_address": "10.0.0.1",
        "private_key": "fake-key",
        "host_type": "shared",
        "total_vcpus": 64,
        "total_ram_mb": 131072,
    }
    defaults.update(kwargs)
    h = Host(**defaults)
    db.add(h)
    db.commit()
    hid = h.id
    db.close()
    return hid


def _create_project_with_host(
    name="proj-with-host",
    state="active",
    topology=None,
    host_kwargs=None,
    **proj_kwargs,
):
    """Create a project with a host_id set, return (project_id, host_id)."""
    hid = _create_host(**(host_kwargs or {}))
    pid = _create_project(
        name=name, state=state, topology=topology, host_id=hid, **proj_kwargs
    )
    return pid, hid


# ---------------------------------------------------------------------------
# VM exec endpoint validation — POST /projects/{id}/vms/{vm_id}/exec
# ---------------------------------------------------------------------------
def test_exec_vm_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{fake_id}/vms/{fake_vm}/exec",
        json={"command": "echo hi"},
    )
    assert resp.status_code == 404


def test_exec_vm_wrong_state():
    pid = _create_project(name="exec-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/exec",
        json={"command": "echo hi"},
    )
    assert resp.status_code == 409
    assert "not accessible" in resp.json()["detail"]


def test_exec_vm_no_host():
    pid = _create_project(name="exec-no-host", state="active")
    fake_vm = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/exec",
        json={"command": "echo hi"},
    )
    assert resp.status_code == 503
    assert "Host not available" in resp.json()["detail"]


def test_exec_vm_no_command():
    pid, _ = _create_project_with_host(name="exec-no-cmd")
    fake_vm = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/exec",
        json={"command": ""},
    )
    assert resp.status_code == 400
    assert "Command is required" in resp.json()["detail"]


def test_exec_vm_project_must_be_active():
    """Exec requires active or stopped state."""
    pid = _create_project(name="exec-deploying", state="deploying")
    fake_vm = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/exec",
        json={"command": "hostname"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# VM stop — POST /projects/{id}/vms/{vm_id}/stop — validation
# ---------------------------------------------------------------------------
def test_stop_vm_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/stop")
    assert resp.status_code == 404


def test_stop_vm_wrong_state():
    pid = _create_project(name="stop-vm-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/stop")
    assert resp.status_code == 409


def test_stop_vm_no_host():
    pid = _create_project(name="stop-vm-no-host", state="active")
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/stop")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# VM start — POST /projects/{id}/vms/{vm_id}/start — validation
# ---------------------------------------------------------------------------
def test_start_vm_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/start")
    assert resp.status_code == 404


def test_start_vm_wrong_state():
    pid = _create_project(name="start-vm-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/start")
    assert resp.status_code == 409


def test_start_vm_no_host():
    pid = _create_project(name="start-vm-no-host", state="active")
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/start")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# VM forcestop — POST /projects/{id}/vms/{vm_id}/forcestop — validation
# ---------------------------------------------------------------------------
def test_forcestop_vm_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/forcestop")
    assert resp.status_code == 404


def test_forcestop_vm_wrong_state():
    pid = _create_project(name="forcestop-vm-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/forcestop")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Disk wipe — POST /projects/{id}/vms/{vm_id}/disks/{disk}/wipe — validation
# ---------------------------------------------------------------------------
def test_wipe_disk_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    fake_disk = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{fake_id}/vms/{fake_vm}/disks/{fake_disk}/wipe"
    )
    assert resp.status_code == 404


def test_wipe_disk_wrong_state():
    pid = _create_project(name="wipe-disk-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    fake_disk = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/disks/{fake_disk}/wipe")
    assert resp.status_code == 409


def test_wipe_disk_no_host():
    pid = _create_project(name="wipe-disk-no-host", state="active")
    fake_vm = str(uuid.uuid4())
    fake_disk = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/disks/{fake_disk}/wipe")
    assert resp.status_code == 503


def test_wipe_disk_kubevirt_vm_not_found():
    db = TestSession()
    host = Host(
        id=str(uuid.uuid4()),
        ip_address="10.0.0.1",
        host_type="kubevirt-cluster",
        state="active",
    )
    db.add(host)
    db.commit()
    topo = {"nodes": [], "edges": []}
    pid = _create_project(
        name="wipe-disk-kv",
        state="active",
        host_id=host.id,
        deployed_topology=topo,
    )
    fake_vm = str(uuid.uuid4())
    fake_disk = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/disks/{fake_disk}/wipe")
    assert resp.status_code == 404
    db.close()


def test_wipe_disk_vm_not_found():
    db = TestSession()
    host = Host(
        id=str(uuid.uuid4()),
        ip_address="10.0.0.2",
        private_key="fake-key",
        state="active",
    )
    db.add(host)
    db.commit()
    topo = {"nodes": [], "edges": []}
    pid = _create_project(
        name="wipe-disk-no-vm",
        state="active",
        host_id=host.id,
        deployed_topology=topo,
    )
    fake_vm = str(uuid.uuid4())
    fake_disk = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/disks/{fake_disk}/wipe")
    assert resp.status_code == 404
    db.close()


def test_wipe_disk_disk_not_found():
    db = TestSession()
    host = Host(
        id=str(uuid.uuid4()),
        ip_address="10.0.0.3",
        private_key="fake-key",
        state="active",
    )
    db.add(host)
    db.commit()
    vm_id = "vm-node-1"
    topo = {
        "nodes": [
            {"id": vm_id, "type": "vmNode", "data": {"name": "test-vm"}},
        ],
        "edges": [],
    }
    pid = _create_project(
        name="wipe-disk-no-disk",
        state="active",
        host_id=host.id,
        deployed_topology=topo,
    )
    fake_disk = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{vm_id}/disks/{fake_disk}/wipe")
    assert resp.status_code == 404
    db.close()


# ---------------------------------------------------------------------------
# VM restart — POST /projects/{id}/vms/{vm_id}/restart — validation
# ---------------------------------------------------------------------------
def test_restart_vm_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/restart")
    assert resp.status_code == 404


def test_restart_vm_wrong_state():
    pid = _create_project(name="restart-vm-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/restart")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# VM console — GET /projects/{id}/vms/{vm_id}/console — validation
# ---------------------------------------------------------------------------
def test_console_vm_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/console")
    assert resp.status_code == 404


def test_console_vm_wrong_state():
    pid = _create_project(name="console-vm-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{pid}/vms/{fake_vm}/console")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# VM status — GET /projects/{id}/vms/{vm_id}/status — validation
# ---------------------------------------------------------------------------
def test_vm_status_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/status")
    assert resp.status_code == 404


def test_vm_status_wrong_state():
    pid = _create_project(name="status-vm-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{pid}/vms/{fake_vm}/status")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# VM ready — GET /projects/{id}/vms/{vm_id}/ready — validation
# ---------------------------------------------------------------------------
def test_vm_ready_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/ready")
    assert resp.status_code == 404


def test_vm_ready_wrong_state():
    pid = _create_project(name="ready-vm-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{pid}/vms/{fake_vm}/ready")
    assert resp.status_code == 409


def test_vm_ready_vm_not_found():
    """VM ready endpoint returns 404 if vm_id is not in topology."""
    topo = {
        "nodes": [{"id": "v1", "type": "vmNode", "data": {"name": "vm1"}}],
        "edges": [],
    }
    pid, _ = _create_project_with_host(name="ready-vm-not-found", topology=topo)
    fake_vm = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{pid}/vms/{fake_vm}/ready")
    assert resp.status_code == 404
    assert "VM not found" in resp.json()["detail"]


def test_vm_ready_no_ip():
    """VM ready returns not ready when VM has no IP configured."""
    vm_id = str(uuid.uuid4())
    topo = {
        "nodes": [
            {
                "id": vm_id,
                "type": "vmNode",
                "data": {"name": "vm1", "nics": [], "ciCloudUserPassword": "pw123"},
            }
        ],
        "edges": [],
    }
    pid, _ = _create_project_with_host(name="ready-vm-no-ip", topology=topo)
    resp = client.get(f"/api/v1/projects/{pid}/vms/{vm_id}/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is False
    assert "no IP" in resp.json()["reason"]


def test_vm_ready_no_password():
    """VM ready returns not ready when VM has no password."""
    vm_id = str(uuid.uuid4())
    topo = {
        "nodes": [
            {
                "id": vm_id,
                "type": "vmNode",
                "data": {
                    "name": "vm1",
                    "nics": [{"id": "nic1", "ip": "192.168.1.10"}],
                    "ciCloudUserPassword": "",
                },
            }
        ],
        "edges": [],
    }
    pid, _ = _create_project_with_host(name="ready-vm-no-pw", topology=topo)
    resp = client.get(f"/api/v1/projects/{pid}/vms/{vm_id}/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is False
    assert "no password" in resp.json()["reason"]


# ---------------------------------------------------------------------------
# VM file upload — PUT /projects/{id}/vms/{vm_id}/files — validation
# ---------------------------------------------------------------------------
def test_upload_file_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.put(
        f"/api/v1/projects/{fake_id}/vms/{fake_vm}/files",
        params={"remote_path": "/tmp/test.txt"},
        files={"file": ("test.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 404


def test_upload_file_wrong_state():
    pid = _create_project(name="upload-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.put(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/files",
        params={"remote_path": "/tmp/test.txt"},
        files={"file": ("test.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# VM file download — GET /projects/{id}/vms/{vm_id}/files — validation
# ---------------------------------------------------------------------------
def test_download_file_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.get(
        f"/api/v1/projects/{fake_id}/vms/{fake_vm}/files",
        params={"remote_path": "/etc/hostname"},
    )
    assert resp.status_code == 404


def test_download_file_wrong_state():
    pid = _create_project(name="download-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.get(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/files",
        params={"remote_path": "/etc/hostname"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Container ops — validation via _get_project_and_host
# ---------------------------------------------------------------------------
def test_container_logs_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_cid = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{fake_id}/containers/{fake_cid}/logs")
    assert resp.status_code == 404


def test_container_logs_wrong_state():
    pid = _create_project(name="cont-logs-draft", state="draft")
    fake_cid = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{pid}/containers/{fake_cid}/logs")
    assert resp.status_code == 409


def test_container_start_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_cid = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/containers/{fake_cid}/start")
    assert resp.status_code == 404


def test_container_start_wrong_state():
    pid = _create_project(name="cont-start-draft", state="draft")
    fake_cid = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/containers/{fake_cid}/start")
    assert resp.status_code == 409


def test_container_stop_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_cid = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/containers/{fake_cid}/stop")
    assert resp.status_code == 404


def test_container_stop_wrong_state():
    pid = _create_project(name="cont-stop-draft", state="draft")
    fake_cid = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/containers/{fake_cid}/stop")
    assert resp.status_code == 409


def test_container_restart_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_cid = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/containers/{fake_cid}/restart")
    assert resp.status_code == 404


def test_container_restart_wrong_state():
    pid = _create_project(name="cont-restart-draft", state="draft")
    fake_cid = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/containers/{fake_cid}/restart")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Reconfigure — deeper validation
# ---------------------------------------------------------------------------
def test_reconfigure_with_host_no_key():
    """Reconfigure returns 503 if host has no private key (non-kubevirt)."""
    hid = _create_host(private_key=None, ip_address="10.0.0.2")
    pid = _create_project(name="reconfig-no-key", state="active", host_id=hid)
    resp = client.post(f"/api/v1/projects/{pid}/reconfigure")
    assert resp.status_code == 503
    assert "Host not available" in resp.json()["detail"]


def test_reconfigure_bmc_no_connected_vm():
    """Reconfigure rejects BMC network with no connected VMs."""
    topo = {
        "nodes": [
            {
                "id": "bmc-net",
                "type": "networkNode",
                "data": {"name": "bmc", "networkType": "bmc", "subtype": "network"},
            },
        ],
        "edges": [],  # No edges — BMC net is disconnected
    }
    pid, _ = _create_project_with_host(
        name="reconfig-bmc-no-vm", state="active", topology=topo
    )
    resp = client.post(f"/api/v1/projects/{pid}/reconfigure")
    assert resp.status_code == 400
    assert "BMC network requires" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Deploy — additional validation paths
# ---------------------------------------------------------------------------
def test_deploy_bmc_no_connected_vm():
    """Deploy rejects topology with BMC network but no connected VM."""
    topo = {
        "nodes": [
            {
                "id": "v1",
                "type": "vmNode",
                "data": {"name": "vm1", "vcpus": 2, "ram": 4},
            },
            {
                "id": "bmc-net",
                "type": "networkNode",
                "data": {"name": "bmc", "networkType": "bmc", "subtype": "network"},
            },
        ],
        "edges": [],  # BMC net not connected to any VM
    }
    pid = _create_project(name="deploy-bmc-no-vm", topology=topo)
    resp = client.post(f"/api/v1/projects/{pid}/deploy")
    assert resp.status_code == 400
    assert "BMC network requires" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Kubeconfigs — access denied path (line 804)
# ---------------------------------------------------------------------------
def test_list_kubeconfigs_multiple_vms():
    """Kubeconfigs returns only VMs that have ocpKubeconfig."""
    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {"name": "sno1", "label": "SNO-1", "ocpKubeconfig": "kc1"},
            },
            {
                "id": "vm2",
                "type": "vmNode",
                "data": {"name": "sno2", "label": "SNO-2"},
            },
            {
                "id": "vm3",
                "type": "vmNode",
                "data": {"name": "sno3", "label": "SNO-3", "ocpKubeconfig": "kc3"},
            },
            {
                "id": "net1",
                "type": "networkNode",
                "data": {"name": "net1"},
            },
        ],
        "edges": [],
    }
    pid = _create_project(name="kc-multi-vms", topology=topo)
    resp = client.get(f"/api/v1/projects/{pid}/kubeconfigs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    vm_ids = {entry["vm_id"] for entry in data}
    assert "vm1" in vm_ids
    assert "vm3" in vm_ids
    assert "vm2" not in vm_ids


def test_list_kubeconfigs_uses_deployed_topology():
    """Kubeconfigs prefers deployed_topology when available."""
    topo = {
        "nodes": [{"id": "vm1", "type": "vmNode", "data": {"name": "draft-vm"}}],
        "edges": [],
    }
    deployed = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "name": "deployed-vm",
                    "label": "Deployed",
                    "ocpKubeconfig": "kc",
                },
            }
        ],
        "edges": [],
    }
    pid = _create_project(
        name="kc-deployed-topo", topology=topo, deployed_topology=deployed
    )
    resp = client.get(f"/api/v1/projects/{pid}/kubeconfigs")
    assert resp.status_code == 200
    data = resp.json()
    # deployed_topology has ocpKubeconfig, topology does not
    assert len(data) == 1
    assert data[0]["vm_name"] == "Deployed"


# ---------------------------------------------------------------------------
# Force stop — more validation
# ---------------------------------------------------------------------------
def test_force_stop_no_host():
    """Force-stop returns 503 when project has no host."""
    pid = _create_project(name="force-stop-no-host", state="active")
    resp = client.post(f"/api/v1/projects/{pid}/force-stop")
    assert resp.status_code == 503
    assert "Host not available" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Redeploy VM — POST /projects/{id}/vms/{vm_id}/redeploy — validation
# ---------------------------------------------------------------------------
def test_redeploy_vm_project_not_found():
    fake_id = str(uuid.uuid4())
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{fake_id}/vms/{fake_vm}/redeploy")
    assert resp.status_code == 404


def test_redeploy_vm_wrong_state():
    pid = _create_project(name="redeploy-vm-draft", state="draft")
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/redeploy")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Redeploy project — additional paths
# ---------------------------------------------------------------------------
def test_redeploy_project_stopped():
    """Redeploy from stopped state returns 400 for no topology."""
    pid = _create_project(
        name="redeploy-stopped-no-topo", state="stopped", topology=None
    )
    resp = client.post(f"/api/v1/projects/{pid}/redeploy")
    assert resp.status_code == 400
    assert "no topology" in resp.json()["detail"]


def test_redeploy_project_error_state_no_vms():
    """Redeploy from error state with empty topology returns 400."""
    pid = _create_project(
        name="redeploy-error-empty",
        state="error",
        topology={"nodes": [], "edges": []},
    )
    resp = client.post(f"/api/v1/projects/{pid}/redeploy")
    assert resp.status_code == 400
    assert "no VMs" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Undeploy — additional paths
# ---------------------------------------------------------------------------
def test_undeploy_active_project_no_host():
    """Undeploy an active project with no host should still work."""
    pid = _create_project(name="undeploy-active-no-host", state="active")
    resp = client.post(f"/api/v1/projects/{pid}/undeploy")
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"


def test_undeploy_stopped_project_no_host():
    """Undeploy a stopped project with no host."""
    pid = _create_project(name="undeploy-stopped-no-host", state="stopped")
    resp = client.post(f"/api/v1/projects/{pid}/undeploy")
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# Delete project — active project without host (draft-like)
# ---------------------------------------------------------------------------
def test_delete_project_active_no_host():
    """Delete an active project with no host_id should succeed as draft delete."""
    pid = _create_project(name="delete-active-no-host", state="active")
    resp = client.delete(f"/api/v1/projects/{pid}")
    assert resp.status_code == 200
    # Verify it's gone
    resp2 = client.get(f"/api/v1/projects/{pid}")
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# Project PATCH — clock_target
# ---------------------------------------------------------------------------
def test_update_project_clock_target():
    pid = _create_project(name="clock-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"clock_target": "2025-01-15T00:00:00Z"},
    )
    assert resp.status_code == 200
    assert resp.json()["clock_target"] is not None
    assert "2025-01-15" in resp.json()["clock_target"]


def test_update_project_clear_clock_target():
    pid = _create_project(name="clock-clear")
    client.patch(
        f"/api/v1/projects/{pid}",
        json={"clock_target": "2025-01-15T00:00:00Z"},
    )
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"clock_target": None},
    )
    assert resp.status_code == 200
    assert resp.json()["clock_target"] is None


# ---------------------------------------------------------------------------
# Project PATCH — host_type
# ---------------------------------------------------------------------------
def test_update_project_host_type():
    pid = _create_project(name="host-type-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"host_type": "dedicated"},
    )
    assert resp.status_code == 200
    assert resp.json()["host_type"] == "dedicated"


# ---------------------------------------------------------------------------
# Project PATCH — poweroff_mode
# ---------------------------------------------------------------------------
def test_update_project_poweroff_mode():
    pid = _create_project(name="poweroff-update")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"poweroff_mode": "parallel"},
    )
    assert resp.status_code == 200
    assert resp.json()["poweroff_mode"] == "parallel"


# ---------------------------------------------------------------------------
# from-template with template_yaml (inline) — validation
# ---------------------------------------------------------------------------
def test_from_template_invalid_bmc_ip():
    """from-template rejects invalid bastion BMC IP."""
    resp = client.post(
        "/api/v1/projects/from-template",
        json={
            "template_yaml": {
                "vms": {"vm1": {"vcpus": 2, "ram": 4096}},
                "networks": {"net1": {"cidr": "192.168.1.0/24"}},
            },
            "bastion_bmc_ip": "not-an-ip",
        },
    )
    assert resp.status_code == 400
    assert "Invalid bastion BMC IP" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Exec with method param — validation
# ---------------------------------------------------------------------------
def test_exec_vm_stopped_state_allowed():
    """Exec is allowed on stopped projects (should pass state check)."""
    pid, _ = _create_project_with_host(name="exec-stopped-proj", state="stopped")
    fake_vm = str(uuid.uuid4())
    # Will fail at troshkad level (503) since there's no real host
    # but should pass the state validation
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/exec",
        json={"command": "echo test", "method": "guest-agent"},
    )
    # We expect 503 (troshkad unreachable), not 409 (state check)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Host with missing private_key/ip — _get_project_and_host
# ---------------------------------------------------------------------------
def test_vm_op_host_no_ip():
    """VM operations return 503 if host has no IP address."""
    hid = _create_host(ip_address=None, private_key="some-key")
    pid = _create_project(name="vm-op-host-no-ip", state="active", host_id=hid)
    fake_vm = str(uuid.uuid4())
    resp = client.post(f"/api/v1/projects/{pid}/vms/{fake_vm}/stop")
    assert resp.status_code == 503
    assert "Host not available" in resp.json()["detail"]


def test_vm_op_host_no_private_key():
    """VM operations return 503 if host has no private key."""
    hid = _create_host(private_key=None, ip_address="10.0.0.3")
    pid = _create_project(name="vm-op-host-no-key", state="active", host_id=hid)
    fake_vm = str(uuid.uuid4())
    resp = client.get(f"/api/v1/projects/{pid}/vms/{fake_vm}/status")
    assert resp.status_code == 503
    assert "Host not available" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Import template — with inline template_yaml containing vms + networks
# ---------------------------------------------------------------------------
def test_import_template_valid():
    """Import a minimal valid template into a draft project."""
    pid = _create_project(name="import-valid")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-template",
        json={
            "template_yaml": {
                "vms": {
                    "vm1": {
                        "vcpus": 2,
                        "ram": 4096,
                        "disks": [{"name": "disk1", "size": 20}],
                    }
                },
                "networks": {"net1": {"cidr": "192.168.1.0/24"}},
            }
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["topology"] is not None
    assert len(data["topology"]["nodes"]) > 0


# ---------------------------------------------------------------------------
# Deploy progress — with different project states
# ---------------------------------------------------------------------------
def test_get_deploy_progress_active():
    pid = _create_project(name="progress-active", state="active")
    resp = client.get(f"/api/v1/projects/{pid}/deploy-progress")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "active"


def test_get_deploy_progress_stopped():
    pid = _create_project(name="progress-stopped", state="stopped")
    resp = client.get(f"/api/v1/projects/{pid}/deploy-progress")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "stopped"


def test_get_deploy_progress_deploying():
    pid = _create_project(name="progress-deploying", state="deploying")
    resp = client.get(f"/api/v1/projects/{pid}/deploy-progress")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "deploying"


# ---------------------------------------------------------------------------
# VM states — with topology nodes but no host
# ---------------------------------------------------------------------------
def test_vm_states_no_host_with_topology():
    topo = {
        "nodes": [
            {"id": "v1", "type": "vmNode", "data": {"name": "vm1"}},
            {"id": "v2", "type": "vmNode", "data": {"name": "vm2"}},
        ],
        "edges": [],
    }
    pid = _create_project(name="vm-states-topo", topology=topo)
    resp = client.get(f"/api/v1/projects/{pid}/vm-states")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"states": {}}


# ---------------------------------------------------------------------------
# from-template with template_yaml — success path
# ---------------------------------------------------------------------------
def test_from_template_inline_yaml():
    """from-template with inline template_yaml creates a project."""
    resp = client.post(
        "/api/v1/projects/from-template",
        json={
            "template_yaml": {
                "vms": {"vm1": {"vcpus": 2, "ram": 4096}},
                "networks": {"net1": {"cidr": "192.168.1.0/24"}},
            },
            "name": f"inline-template-{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "name" in data


# ---------------------------------------------------------------------------
# Export — with deployed_topology (uses deployed over topology)
# ---------------------------------------------------------------------------
def test_export_template_with_clock_target():
    """Export includes clock_target when set on project."""
    import datetime

    topo = {
        "nodes": [
            {"id": "v1", "type": "vmNode", "data": {"name": "vm-clock"}},
        ],
        "edges": [],
    }
    pid = _create_project(name="export-clock", topology=topo)
    # Set clock_target via DB
    db = TestSession()
    project = db.query(Project).filter_by(id=pid).first()
    project.clock_target = datetime.datetime(2025, 1, 15, tzinfo=datetime.UTC)
    db.commit()
    db.close()
    resp = client.post(f"/api/v1/projects/{pid}/export-template")
    assert resp.status_code == 200
    assert "clock_target" in resp.text
    assert "2025-01-15" in resp.text


# ---------------------------------------------------------------------------
# Topology update — single bastion browser OK
# ---------------------------------------------------------------------------
def test_update_topology_single_bastion_browser_ok():
    """Topology with single bastion browser should succeed."""
    pid = _create_project(name="bastion-single-ok")
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
                "data": {"name": "vm2", "configureBastionBrowser": False},
            },
        ],
        "edges": [],
    }
    resp = client.patch(f"/api/v1/projects/{pid}", json={"topology": topo})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Exec — method parameter variants
# ---------------------------------------------------------------------------
def test_exec_use_ssh_flag():
    """use_ssh flag should be interpreted as method=ssh."""
    pid, _ = _create_project_with_host(name="exec-use-ssh")
    fake_vm = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/exec",
        json={"command": "whoami", "use_ssh": True},
    )
    # Should fail at troshkad level, not at validation
    assert resp.status_code == 503


def test_exec_console_text_method():
    """console-text method should work (maps to console with force_tty)."""
    pid, _ = _create_project_with_host(name="exec-console-text")
    fake_vm = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/{fake_vm}/exec",
        json={"command": "whoami", "method": "console-text"},
    )
    # Should fail at troshkad level, not at validation
    assert resp.status_code == 503
