"""Tests for project API endpoint handlers (import, export, kubeconfigs, exec, patch)."""

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.project import Project
from app.models.user import User
from app.services.troshkad_client import TroshkadError
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------
_db = TestSession()
_user = User(
    email="projep-test@example.com",
    display_name="EP Test",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

_user2 = User(
    email="projep-other@example.com",
    display_name="Other User",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user2)
_db.commit()
_db.refresh(_user2)
TOKEN_OTHER = create_jwt(user_id=_user2.id, email=_user2.email, role=_user2.role)
HEADERS_OTHER = {"Authorization": f"Bearer {TOKEN_OTHER}"}
_db.close()


def _create_draft_project(name="EP Test", topology=None) -> str:
    """Create a draft project and return its ID."""
    db = TestSession()
    p = Project(
        name=name,
        owner_id=USER_ID,
        state="draft",
        topology=topology or {"nodes": [], "edges": []},
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    pid = p.id
    db.close()
    return pid


def _create_active_project_with_host(topology=None):
    """Create an active project with a host and return (project_id, host_id)."""
    db = TestSession()
    host = Host(
        id=str(uuid.uuid4()),
        state="running",
        host_type="shared",
        ip_address="10.0.0.1",
        private_key="fake-ssh-key",
        agent_status="connected",
        agent_token="fake-token",
    )
    db.add(host)
    db.flush()

    topo = topology or {
        "nodes": [
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "label": "test-vm",
                    "nics": [{"ip": "192.168.1.10"}],
                    "ciCloudUserPassword": "testpass",
                },
            }
        ],
        "edges": [],
    }
    p = Project(
        name="Active EP Test",
        owner_id=USER_ID,
        state="active",
        host_id=host.id,
        topology=topo,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    pid = p.id
    hid = host.id
    db.close()
    return pid, hid


# ===================================================================
# POST /projects/{id}/import-template
# ===================================================================


class TestImportTemplate:
    def test_import_minimal_template(self):
        pid = _create_draft_project()
        template = {
            "vms": {
                "bastion": {
                    "vcpus": 2,
                    "ram_gb": 4,
                    "disks": [{"size_gb": 20}],
                    "nics": [{"network": "internal"}],
                }
            },
            "networks": {
                "internal": {
                    "cidr": "192.168.47.0/24",
                }
            },
        }
        mock_topo = {
            "nodes": [{"id": "n1", "type": "vmNode", "data": {"label": "bastion"}}],
            "edges": [],
        }
        with patch(
            "app.services.template_loader.resolve_inline_template",
            side_effect=lambda x: x,
        ), patch(
            "app.services.template_loader.generate_topology_from_template",
            return_value=mock_topo,
        ) as mock_gen, patch(
            "app.services.deploy_topology.validate_topology_names",
            return_value=[],
        ), patch(
            "app.services.deploy_topology.validate_topology_ips",
            return_value=[],
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/import-template",
                json={"template_yaml": template},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "topology" in data
        assert data["topology"]["nodes"][0]["id"] == "n1"
        mock_gen.assert_called_once()

    def test_import_missing_template_yaml(self):
        pid = _create_draft_project()
        resp = client.post(
            f"/api/v1/projects/{pid}/import-template",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "template_yaml is required" in resp.json()["detail"]

    def test_import_missing_vms_section(self):
        pid = _create_draft_project()
        resp = client.post(
            f"/api/v1/projects/{pid}/import-template",
            json={"template_yaml": {"networks": {"net1": {"cidr": "10.0.0.0/24"}}}},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "vms" in resp.json()["detail"]

    def test_import_missing_networks_section(self):
        pid = _create_draft_project()
        resp = client.post(
            f"/api/v1/projects/{pid}/import-template",
            json={"template_yaml": {"vms": {"vm1": {"vcpus": 1}}}},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "networks" in resp.json()["detail"]

    def test_import_non_draft_project(self):
        db = TestSession()
        p = Project(name="Active Proj", owner_id=USER_ID, state="active")
        db.add(p)
        db.commit()
        db.refresh(p)
        pid = p.id
        db.close()

        resp = client.post(
            f"/api/v1/projects/{pid}/import-template",
            json={
                "template_yaml": {
                    "vms": {"vm1": {}},
                    "networks": {"net1": {}},
                }
            },
            headers=HEADERS,
        )
        assert resp.status_code == 409
        assert "draft" in resp.json()["detail"]

    def test_import_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/projects/{fake_id}/import-template",
            json={"template_yaml": {"vms": {}, "networks": {}}},
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_import_template_yaml_not_dict(self):
        pid = _create_draft_project()
        resp = client.post(
            f"/api/v1/projects/{pid}/import-template",
            json={"template_yaml": "not-a-dict"},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "YAML mapping" in resp.json()["detail"]


# ===================================================================
# POST /projects/{id}/export-template
# ===================================================================


class TestExportTemplate:
    def test_export_basic(self):
        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"label": "bastion", "nics": []},
                }
            ],
            "edges": [],
        }
        pid = _create_draft_project(topology=topo)

        with patch(
            "app.services.template_loader.export_topology_to_template",
            return_value={"vms": {"bastion": {"vcpus": 2}}, "networks": {}},
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/export-template",
                json={},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/yaml; charset=utf-8"
        assert "bastion" in resp.text

    def test_export_password_mode_none(self):
        pid = _create_draft_project()
        with patch(
            "app.services.template_loader.export_topology_to_template",
            return_value={
                "vms": {"vm1": {"cloud_user_password": "secret"}},
                "networks": {"net1": {"bmc_password": "bmc-secret"}},
            },
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/export-template",
                json={"password_mode": "none"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        assert "secret" not in resp.text
        assert "Passwords omitted" in resp.text

    def test_export_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/projects/{fake_id}/export-template",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_export_access_denied(self):
        pid = _create_draft_project()
        resp = client.post(
            f"/api/v1/projects/{pid}/export-template",
            json={},
            headers=HEADERS_OTHER,
        )
        assert resp.status_code == 403


# ===================================================================
# GET /projects/{id}/kubeconfigs
# ===================================================================


class TestListKubeconfigs:
    def test_kubeconfigs_with_ocp_vm(self):
        topo = {
            "nodes": [
                {
                    "id": "vm-sno",
                    "type": "vmNode",
                    "data": {
                        "label": "sno",
                        "ocpKubeconfig": "apiVersion: v1\nclusters: ...",
                    },
                },
                {
                    "id": "vm-bastion",
                    "type": "vmNode",
                    "data": {"label": "bastion"},
                },
            ],
            "edges": [],
        }
        pid = _create_draft_project(topology=topo)
        resp = client.get(
            f"/api/v1/projects/{pid}/kubeconfigs",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["vm_name"] == "sno"
        assert data[0]["vm_id"] == "vm-sno"

    def test_kubeconfigs_empty(self):
        pid = _create_draft_project()
        resp = client.get(
            f"/api/v1/projects/{pid}/kubeconfigs",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_kubeconfigs_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.get(
            f"/api/v1/projects/{fake_id}/kubeconfigs",
            headers=HEADERS,
        )
        assert resp.status_code == 404


# ===================================================================
# POST /projects/{id}/vms/{vm_id}/exec
# ===================================================================


class TestVmExec:
    def test_exec_guest_agent_success(self):
        pid, hid = _create_active_project_with_host()
        # start_job returns a string job_id
        # wait_for_job returns the job dict with result.output (not stdout)
        mock_job = {
            "job_id": "j1",
            "status": "completed",
            "result": {"output": "hello", "error": "", "exit_code": 0},
        }
        with patch("app.api.projects.start_job", return_value="j1"), patch(
            "app.api.projects.wait_for_job", return_value=mock_job
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/vms/vm-1/exec",
                json={"command": "echo hello", "method": "guest-agent"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["output"] == "hello"
        assert data["exit_code"] == 0
        assert data["method"] == "guest-agent"

    def test_exec_missing_command(self):
        pid, _ = _create_active_project_with_host()
        resp = client.post(
            f"/api/v1/projects/{pid}/vms/vm-1/exec",
            json={"command": ""},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "Command is required" in resp.json()["detail"]

    def test_exec_project_not_active(self):
        pid = _create_draft_project()
        resp = client.post(
            f"/api/v1/projects/{pid}/vms/vm-1/exec",
            json={"command": "ls"},
            headers=HEADERS,
        )
        # Draft project -> _get_project_and_host returns 409 for non-active
        assert resp.status_code == 409

    def test_exec_project_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/projects/{fake_id}/vms/vm-1/exec",
            json={"command": "ls"},
            headers=HEADERS,
        )
        assert resp.status_code == 404


# ===================================================================
# PATCH /projects/{id} -- topology update triggers notify
# ===================================================================


class TestUpdateProject:
    def test_patch_name(self):
        pid = _create_draft_project(name="Original Name")
        resp = client.patch(
            f"/api/v1/projects/{pid}",
            json={"name": "New Name"},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_patch_topology_triggers_notify(self):
        pid = _create_draft_project()
        new_topo = {
            "nodes": [{"id": "n1", "type": "vmNode", "data": {"label": "vm1"}}],
            "edges": [],
        }
        with patch("app.api.projects.notify_project") as mock_notify:
            resp = client.patch(
                f"/api/v1/projects/{pid}",
                json={"topology": new_topo},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["topology"]["nodes"][0]["id"] == "n1"
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert call_args[0][0] == pid
        assert call_args[0][1]["type"] == "topology-update"

    def test_patch_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.patch(
            f"/api/v1/projects/{fake_id}",
            json={"name": "X"},
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_patch_access_denied(self):
        pid = _create_draft_project()
        resp = client.patch(
            f"/api/v1/projects/{pid}",
            json={"name": "Hacked"},
            headers=HEADERS_OTHER,
        )
        assert resp.status_code == 403

    def test_patch_guest_exec(self):
        pid = _create_draft_project()
        resp = client.patch(
            f"/api/v1/projects/{pid}",
            json={"guest_exec_enabled": False},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["guest_exec_enabled"] is False

    def test_patch_clock_target_on_active(self):
        pid, _ = _create_active_project_with_host()
        with patch("app.services.clock_service.adjust_clocks_async") as mock_clock:
            resp = client.patch(
                f"/api/v1/projects/{pid}",
                json={"clock_target": "2025-06-01T00:00:00+00:00"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        mock_clock.assert_called_once_with(pid)


# ===================================================================
# _build_pull_through_config
# ===================================================================


class TestBuildPullThroughConfig:
    def test_returns_correct_structure(self):
        from app.api.projects import _build_pull_through_config

        result = _build_pull_through_config("https://mirror.example.com")
        assert result["enabled"] is True
        assert result["url"] == "https://mirror.example.com"
        assert result["orgs"]["registry.redhat.io"] == "registry_redhat_io"
        assert result["orgs"]["quay.io"] == "quay_io"

    def test_different_url(self):
        from app.api.projects import _build_pull_through_config

        result = _build_pull_through_config("https://other.registry.io:5000")
        assert result["url"] == "https://other.registry.io:5000"
        assert result["enabled"] is True
        assert len(result["orgs"]) == 2

    def test_empty_url(self):
        from app.api.projects import _build_pull_through_config

        result = _build_pull_through_config("")
        assert result["enabled"] is True
        assert result["url"] == ""


# ===================================================================
# _resolve_deploy_progress
# ===================================================================


class TestResolveDeployProgress:
    def test_non_transitional_state_returns_none(self):
        from app.api.projects import _resolve_deploy_progress

        db = TestSession()
        p = Project(name="DP Draft", owner_id=USER_ID, state="draft")
        db.add(p)
        db.commit()
        db.refresh(p)
        result = _resolve_deploy_progress(p)
        db.close()
        assert result is None

    def test_active_state_returns_none(self):
        from app.api.projects import _resolve_deploy_progress

        db = TestSession()
        p = Project(name="DP Active", owner_id=USER_ID, state="active")
        db.add(p)
        db.commit()
        db.refresh(p)
        result = _resolve_deploy_progress(p)
        db.close()
        assert result is None

    def test_deploying_with_progress_data(self):
        from app.api.projects import _resolve_deploy_progress

        db = TestSession()
        p = Project(name="DP Deploying", owner_id=USER_ID, state="deploying")
        db.add(p)
        db.commit()
        db.refresh(p)

        progress = {"step": "downloading", "detail": "50%"}
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=progress,
        ):
            result = _resolve_deploy_progress(p)
        db.close()
        assert result == progress
        assert result["step"] == "downloading"

    def test_deploying_queued_job(self):
        from app.api.projects import _resolve_deploy_progress

        db = TestSession()
        p = Project(name="DP Queued", owner_id=USER_ID, state="deploying")
        db.add(p)
        db.commit()
        db.refresh(p)

        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=None,
        ), patch(
            "app.core.redis.get_job_info",
            return_value={
                "status": "queued",
                "queue_position": 2,
                "queue_length": 5,
            },
        ):
            result = _resolve_deploy_progress(p)
        db.close()
        assert result is not None
        assert result["step"] == "queued"
        assert "#2" in result["detail"]
        assert "5" in result["detail"]

    def test_stopping_state_returns_progress(self):
        from app.api.projects import _resolve_deploy_progress

        db = TestSession()
        p = Project(name="DP Stopping", owner_id=USER_ID, state="stopping")
        db.add(p)
        db.commit()
        db.refresh(p)

        progress = {"step": "stopping_vms", "detail": "Shutting down"}
        with patch(
            "app.services.deploy_service._get_deploy_progress_data",
            return_value=progress,
        ):
            result = _resolve_deploy_progress(p)
        db.close()
        assert result == progress


# ===================================================================
# POST /projects/ — duplicate name
# ===================================================================


class TestCreateProjectDuplicate:
    def test_duplicate_name_returns_409(self):
        unique = f"Dup Test {uuid.uuid4().hex[:8]}"
        # Create first project
        resp1 = client.post(
            "/api/v1/projects/",
            json={"name": unique},
            headers=HEADERS,
        )
        assert resp1.status_code == 201

        # Try to create another with same name
        resp2 = client.post(
            "/api/v1/projects/",
            json={"name": unique},
            headers=HEADERS,
        )
        assert resp2.status_code == 409
        assert "already have a project" in resp2.json()["detail"]

    def test_different_users_can_have_same_name(self):
        unique = f"SameName {uuid.uuid4().hex[:8]}"
        resp1 = client.post(
            "/api/v1/projects/",
            json={"name": unique},
            headers=HEADERS,
        )
        assert resp1.status_code == 201

        # Other user should be able to create same name
        resp2 = client.post(
            "/api/v1/projects/",
            json={"name": unique},
            headers=HEADERS_OTHER,
        )
        assert resp2.status_code == 201

    def test_create_project_minimal(self):
        unique = f"Minimal {uuid.uuid4().hex[:8]}"
        resp = client.post(
            "/api/v1/projects/",
            json={"name": unique},
            headers=HEADERS,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == unique
        assert data["state"] == "draft"


# ===================================================================
# GET /projects/templates
# ===================================================================


class TestListTemplates:
    def test_list_templates(self):
        with patch(
            "app.services.template_loader.list_yaml_templates",
            return_value=[
                {"id": "sno", "name": "Single Node OCP"},
                {"id": "compact", "name": "Compact OCP"},
            ],
        ):
            resp = client.get("/api/v1/projects/templates", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == "sno"

    def test_list_templates_empty(self):
        with patch(
            "app.services.template_loader.list_yaml_templates",
            return_value=[],
        ):
            resp = client.get("/api/v1/projects/templates", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_templates_with_auth(self):
        with patch(
            "app.services.template_loader.list_yaml_templates",
            return_value=[{"id": "sno", "name": "SNO"}],
        ):
            resp = client.get("/api/v1/projects/templates", headers=HEADERS)
        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ===================================================================
# POST /projects/auto-layout
# ===================================================================


class TestAutoLayout:
    def test_auto_layout_returns_nodes_and_edges(self):
        nodes = [{"id": "n1", "type": "vmNode", "position": {"x": 0, "y": 0}}]
        edges = [{"id": "e1", "source": "n1", "target": "n2"}]
        laid_out_nodes = [
            {"id": "n1", "type": "vmNode", "position": {"x": 100, "y": 200}}
        ]
        laid_out_edges = [{"id": "e1", "source": "n1", "target": "n2"}]

        with patch(
            "app.services.auto_layout.auto_layout",
            return_value=(laid_out_nodes, laid_out_edges),
        ):
            resp = client.post(
                "/api/v1/projects/auto-layout",
                json={"nodes": nodes, "edges": edges},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert data["nodes"][0]["position"]["x"] == 100

    def test_auto_layout_empty_input(self):
        with patch(
            "app.services.auto_layout.auto_layout",
            return_value=([], []),
        ):
            resp = client.post(
                "/api/v1/projects/auto-layout",
                json={},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes"] == []
        assert data["edges"] == []

    def test_auto_layout_with_nodes_only(self):
        nodes = [{"id": "n1", "type": "vmNode", "position": {"x": 0, "y": 0}}]
        with patch(
            "app.services.auto_layout.auto_layout",
            return_value=(nodes, []),
        ):
            resp = client.post(
                "/api/v1/projects/auto-layout",
                json={"nodes": nodes},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        assert len(resp.json()["nodes"]) == 1
        assert resp.json()["edges"] == []


# ===================================================================
# POST /projects/from-template
# ===================================================================


class TestCreateFromTemplate:
    def test_from_template_with_inline_yaml(self):
        template = {
            "name": "test-template",
            "vms": {
                "bastion": {
                    "vcpus": 2,
                    "ram_gb": 4,
                    "disks": [{"size_gb": 20}],
                    "nics": [{"network": "internal"}],
                }
            },
            "networks": {
                "internal": {"cidr": "192.168.47.0/24"},
            },
        }
        mock_topo = {
            "nodes": [{"id": "n1", "type": "vmNode", "data": {"label": "bastion"}}],
            "edges": [],
        }
        with patch(
            "app.services.template_loader.resolve_inline_template",
            side_effect=lambda x: x,
        ), patch(
            "app.services.template_loader.generate_topology_from_template",
            return_value=mock_topo,
        ), patch(
            "app.services.deploy_topology.validate_topology_names",
            return_value=[],
        ), patch(
            "app.services.deploy_topology.validate_topology_ips",
            return_value=[],
        ):
            resp = client.post(
                "/api/v1/projects/from-template",
                json={
                    "template_yaml": template,
                    "name": f"From Template {uuid.uuid4().hex[:8]}",
                },
                headers=HEADERS,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "name" in data

    def test_from_template_missing_both_ids(self):
        resp = client.post(
            "/api/v1/projects/from-template",
            json={"name": "NoTemplate"},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "template_id or template_yaml is required" in resp.json()["detail"]

    def test_from_template_not_found(self):
        with patch(
            "app.services.template_loader.resolve_template",
            side_effect=FileNotFoundError("not found"),
        ):
            resp = client.post(
                "/api/v1/projects/from-template",
                json={"template_id": "nonexistent-template"},
                headers=HEADERS,
            )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_from_template_invalid_bmc_ip(self):
        template = {
            "name": "test-bmc-ip",
            "vms": {"vm1": {"vcpus": 1}},
            "networks": {"net1": {"cidr": "10.0.0.0/24"}},
        }
        with patch(
            "app.services.template_loader.resolve_inline_template",
            side_effect=lambda x: x,
        ), patch(
            "app.services.template_loader.generate_topology_from_template",
            return_value={"nodes": [], "edges": []},
        ), patch(
            "app.services.deploy_topology.validate_topology_names",
            return_value=[],
        ), patch(
            "app.services.deploy_topology.validate_topology_ips",
            return_value=[],
        ):
            resp = client.post(
                "/api/v1/projects/from-template",
                json={
                    "template_yaml": template,
                    "name": f"Bad BMC {uuid.uuid4().hex[:8]}",
                    "bastion_bmc_ip": "not-an-ip",
                },
                headers=HEADERS,
            )
        assert resp.status_code == 400
        assert "Invalid bastion BMC IP" in resp.json()["detail"]

    def test_from_template_topology_validation_errors(self):
        template = {
            "name": "dup-names",
            "vms": {"vm1": {"vcpus": 1}},
            "networks": {"net1": {"cidr": "10.0.0.0/24"}},
        }
        with patch(
            "app.services.template_loader.resolve_inline_template",
            side_effect=lambda x: x,
        ), patch(
            "app.services.template_loader.generate_topology_from_template",
            return_value={"nodes": [], "edges": []},
        ), patch(
            "app.services.deploy_topology.validate_topology_names",
            return_value=["Duplicate VM name: vm1"],
        ), patch(
            "app.services.deploy_topology.validate_topology_ips",
            return_value=[],
        ):
            resp = client.post(
                "/api/v1/projects/from-template",
                json={
                    "template_yaml": template,
                    "name": f"DupNames {uuid.uuid4().hex[:8]}",
                },
                headers=HEADERS,
            )
        assert resp.status_code == 400
        assert "duplicate names" in resp.json()["detail"].lower()


# ===================================================================
# POST /projects/{id}/import-template — additional coverage
# ===================================================================


class TestImportTemplateAdditional:
    def test_import_with_clock_target(self):
        pid = _create_draft_project()
        template = {
            "vms": {
                "vm1": {
                    "vcpus": 2,
                    "ram_gb": 4,
                    "disks": [{"size_gb": 20}],
                    "nics": [{"network": "net1"}],
                }
            },
            "networks": {"net1": {"cidr": "10.0.0.0/24"}},
            "clock_target": "2025-01-15T00:00:00Z",
        }
        mock_topo = {
            "nodes": [{"id": "n1", "type": "vmNode", "data": {"label": "vm1"}}],
            "edges": [],
        }
        with patch(
            "app.services.template_loader.resolve_inline_template",
            side_effect=lambda x: x,
        ), patch(
            "app.services.template_loader.generate_topology_from_template",
            return_value=mock_topo,
        ), patch(
            "app.services.deploy_topology.validate_topology_names",
            return_value=[],
        ), patch(
            "app.services.deploy_topology.validate_topology_ips",
            return_value=[],
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/import-template",
                json={"template_yaml": template},
                headers=HEADERS,
            )
        assert resp.status_code == 200

    def test_import_topology_validation_errors(self):
        pid = _create_draft_project()
        template = {
            "vms": {"vm1": {"vcpus": 1, "nics": [{"network": "net1"}]}},
            "networks": {"net1": {"cidr": "10.0.0.0/24"}},
        }
        with patch(
            "app.services.template_loader.resolve_inline_template",
            side_effect=lambda x: x,
        ), patch(
            "app.services.template_loader.generate_topology_from_template",
            return_value={"nodes": [], "edges": []},
        ), patch(
            "app.services.deploy_topology.validate_topology_names",
            return_value=["Duplicate VM: vm1"],
        ), patch(
            "app.services.deploy_topology.validate_topology_ips",
            return_value=[],
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/import-template",
                json={"template_yaml": template},
                headers=HEADERS,
            )
        assert resp.status_code == 400
        assert "duplicate names" in resp.json()["detail"].lower()

    def test_import_invalid_template_raises_400(self):
        pid = _create_draft_project()
        template = {
            "vms": {"vm1": {"vcpus": 1, "nics": [{"network": "net1"}]}},
            "networks": {"net1": {"cidr": "10.0.0.0/24"}},
        }
        with patch(
            "app.services.template_loader.resolve_inline_template",
            side_effect=ValueError("bad template format"),
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/import-template",
                json={"template_yaml": template},
                headers=HEADERS,
            )
        assert resp.status_code == 400
        assert "Invalid template" in resp.json()["detail"]

    def test_import_access_denied(self):
        pid = _create_draft_project()
        template = {
            "vms": {"vm1": {}},
            "networks": {"net1": {}},
        }
        resp = client.post(
            f"/api/v1/projects/{pid}/import-template",
            json={"template_yaml": template},
            headers=HEADERS_OTHER,
        )
        assert resp.status_code == 403


# ===================================================================
# Container lifecycle endpoints
# ===================================================================


class TestContainerLifecycle:
    """Tests for container start/stop/restart/logs endpoints."""

    def test_start_container(self):
        pid, _hid = _create_active_project_with_host()
        cid = str(uuid.uuid4())
        with patch("app.api.projects.start_job", return_value="job-1"), patch(
            "app.api.projects.wait_for_job",
            return_value={"status": "completed", "result": {}},
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/containers/{cid}/start",
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert cid[:8] in data["container_name"]

    def test_stop_container(self):
        pid, _hid = _create_active_project_with_host()
        cid = str(uuid.uuid4())
        with patch("app.api.projects.start_job", return_value="job-1"), patch(
            "app.api.projects.wait_for_job",
            return_value={"status": "completed", "result": {}},
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/containers/{cid}/stop",
                headers=HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

    def test_restart_container_calls_stop_then_start(self):
        pid, _hid = _create_active_project_with_host()
        cid = str(uuid.uuid4())
        with patch(
            "app.api.projects.start_job", return_value="job-1"
        ) as mock_sj, patch(
            "app.api.projects.wait_for_job",
            return_value={"status": "completed", "result": {}},
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/containers/{cid}/restart",
                headers=HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "restarted"
        # restart calls start_job twice: once for stop, once for start
        assert mock_sj.call_count == 2
        calls = mock_sj.call_args_list
        assert "/containers/stop" in str(calls[0])
        assert "/containers/start" in str(calls[1])

    def test_redeploy_container_enqueues_job(self):
        pid, _hid = _create_active_project_with_host()
        cid = str(uuid.uuid4())
        with patch(
            "app.services.deploy_topology._extract_containers",
            return_value=[{"node_id": cid, "name": "showroom", "is_pod": True}],
        ), patch("app.core.redis.enqueue_job") as mock_enq:
            resp = client.post(
                f"/api/v1/projects/{pid}/containers/{cid}/redeploy",
                headers=HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "redeploying"
        mock_enq.assert_called_once()
        # enqueued with (job_fn, project_id, container_id)
        args = mock_enq.call_args[0]
        assert args[1] == pid and args[2] == cid

    def test_redeploy_container_not_found(self):
        pid, _hid = _create_active_project_with_host()
        with patch(
            "app.services.deploy_topology._extract_containers", return_value=[]
        ), patch("app.core.redis.enqueue_job") as mock_enq:
            resp = client.post(
                f"/api/v1/projects/{pid}/containers/{uuid.uuid4()}/redeploy",
                headers=HEADERS,
            )
        assert resp.status_code == 404
        mock_enq.assert_not_called()

    def test_get_container_logs(self):
        pid, _hid = _create_active_project_with_host()
        cid = str(uuid.uuid4())
        with patch("app.api.projects.start_job", return_value="job-1"), patch(
            "app.api.projects.wait_for_job",
            return_value={
                "status": "completed",
                "result": {"logs": "line1\nline2\n"},
            },
        ):
            resp = client.get(
                f"/api/v1/projects/{pid}/containers/{cid}/logs?tail=50",
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "line1" in data["logs"]
        assert cid[:8] in data["container_name"]

    def test_start_container_troshkad_error(self):
        pid, _hid = _create_active_project_with_host()
        cid = str(uuid.uuid4())
        with patch(
            "app.api.projects.start_job",
            side_effect=TroshkadError("connection refused"),
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/containers/{cid}/start",
                headers=HEADERS,
            )
        assert resp.status_code == 503
        assert "connection refused" in resp.json()["detail"]


# ===================================================================
# VM exec endpoints -- SSH, serial, console, auto
# ===================================================================


class TestVmExecExtended:
    """Tests for POST /projects/{id}/vms/{vm_id}/exec with various methods."""

    _EXEC_TOPO = {
        "nodes": [
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "label": "test-vm",
                    "nics": [{"ip": "192.168.1.10"}],
                    "ciCloudUserPassword": "testpass",
                    "ciRootPassword": "rootpass",
                    "cloudInit": {"userData": "#cloud-config"},
                },
            }
        ],
        "edges": [],
    }

    def test_exec_ssh_method(self):
        pid, _hid = _create_active_project_with_host(topology=self._EXEC_TOPO)
        with patch("app.api.projects.start_job", return_value="job-1"), patch(
            "app.api.projects.wait_for_job",
            return_value={
                "status": "completed",
                "result": {"output": "hello", "error": "", "exit_code": 0},
            },
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/vms/vm-1/exec",
                json={"command": "echo hello", "method": "ssh"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["output"] == "hello"
        assert data["method"] == "ssh"
        assert data["exit_code"] == 0

    def test_exec_serial_method(self):
        pid, _hid = _create_active_project_with_host(topology=self._EXEC_TOPO)
        with patch("app.api.projects.start_job", return_value="job-1"), patch(
            "app.api.projects.wait_for_job",
            return_value={
                "status": "completed",
                "result": {"output": "serial-out", "error": ""},
            },
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/vms/vm-1/exec",
                json={"command": "uname", "method": "serial"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["output"] == "serial-out"
        assert data["method"] == "serial"

    def test_exec_console_method(self):
        pid, _hid = _create_active_project_with_host(topology=self._EXEC_TOPO)
        with patch("app.api.projects.start_job", return_value="job-1"), patch(
            "app.api.projects.wait_for_job",
            return_value={
                "status": "completed",
                "result": {"output": "console-out", "error": "", "exit_code": 0},
            },
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/vms/vm-1/exec",
                json={"command": "whoami", "method": "console"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["output"] == "console-out"
        assert data["method"] == "console"

    def test_exec_auto_fallback_guest_agent_fails_ssh_succeeds(self):
        pid, _hid = _create_active_project_with_host(topology=self._EXEC_TOPO)
        call_count = {"n": 0}

        def mock_wait(host, job_id, timeout=60):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # guest-agent fails
                return {"status": "failed", "result": {"error": "ga not running"}}
            # ssh succeeds
            return {
                "status": "completed",
                "result": {"output": "ok", "error": "", "exit_code": 0},
            }

        with patch("app.api.projects.start_job", return_value="job-1"), patch(
            "app.api.projects.wait_for_job", side_effect=mock_wait
        ):
            resp = client.post(
                f"/api/v1/projects/{pid}/vms/vm-1/exec",
                json={"command": "ls", "method": "auto"},
                headers=HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["method"] == "ssh"

    def test_exec_missing_command(self):
        pid, _hid = _create_active_project_with_host()
        resp = client.post(
            f"/api/v1/projects/{pid}/vms/vm-1/exec",
            json={"command": ""},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "Command is required" in resp.json()["detail"]

    def test_exec_wrong_state(self):
        pid = _create_draft_project()
        resp = client.post(
            f"/api/v1/projects/{pid}/vms/vm-1/exec",
            json={"command": "ls"},
            headers=HEADERS,
        )
        # draft project -> 409 from _get_project_and_host
        assert resp.status_code == 409

    def test_exec_console_no_password(self):
        """Console method with no password should fail with all-methods-failed."""
        topo_no_pass = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "label": "test-vm",
                        "nics": [{"ip": "192.168.1.10"}],
                    },
                }
            ],
            "edges": [],
        }
        pid, _hid = _create_active_project_with_host(topology=topo_no_pass)
        resp = client.post(
            f"/api/v1/projects/{pid}/vms/vm-1/exec",
            json={"command": "whoami", "method": "console"},
            headers=HEADERS,
        )
        assert resp.status_code == 503
        assert "no password" in resp.json()["detail"].lower()


# ===================================================================
# Reconfigure endpoint
# ===================================================================


class TestReconfigure:
    """Tests for POST /projects/{id}/reconfigure."""

    def test_reconfigure_wrong_state(self):
        pid = _create_draft_project()
        resp = client.post(
            f"/api/v1/projects/{pid}/reconfigure",
            headers=HEADERS,
        )
        assert resp.status_code == 409
        assert "draft" in resp.json()["detail"]

    def test_reconfigure_deploying_state(self):
        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="running",
            host_type="shared",
            ip_address="10.0.0.1",
            private_key="fake-ssh-key",
            agent_status="connected",
            agent_token="fake-token",
        )
        db.add(host)
        db.flush()
        p = Project(
            name="Reconfig Deploying",
            owner_id=USER_ID,
            state="deploying",
            host_id=host.id,
            topology={"nodes": [], "edges": []},
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        pid = p.id
        db.close()

        resp = client.post(
            f"/api/v1/projects/{pid}/reconfigure",
            headers=HEADERS,
        )
        assert resp.status_code == 409
        assert "deploying" in resp.json()["detail"]

    def test_reconfigure_no_host(self):
        db = TestSession()
        p = Project(
            name="Reconfig No Host",
            owner_id=USER_ID,
            state="active",
            host_id=None,
            topology={"nodes": [], "edges": []},
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        pid = p.id
        db.close()

        resp = client.post(
            f"/api/v1/projects/{pid}/reconfigure",
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "no active deployment" in resp.json()["detail"].lower()

    def test_reconfigure_success(self):
        pid, _hid = _create_active_project_with_host()
        with patch(
            "app.api.projects.diff_topologies",
            return_value={
                "added_vms": [],
                "removed_vms": [],
                "changed_vms": [],
                "added_networks": [],
                "removed_networks": [],
                "has_changes": False,
            },
        ), patch("app.core.redis.enqueue_job"):
            resp = client.post(
                f"/api/v1/projects/{pid}/reconfigure",
                headers=HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "reconfiguring"

    def test_reconfigure_access_denied(self):
        pid, _hid = _create_active_project_with_host()
        resp = client.post(
            f"/api/v1/projects/{pid}/reconfigure",
            headers=HEADERS_OTHER,
        )
        assert resp.status_code == 403


# ===================================================================
# Helper functions -- direct calls
# ===================================================================


class TestFindGatewayNode:
    """Tests for _find_gateway_node()."""

    def test_finds_gateway(self):
        from app.api.projects import _find_gateway_node

        topo = {
            "nodes": [
                {
                    "id": "gw-1",
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "gatewayMode": "nat-portforward",
                    },
                },
                {"id": "vm-1", "type": "vmNode", "data": {}},
            ]
        }
        result = _find_gateway_node(topo)
        assert result is not None
        assert result["id"] == "gw-1"

    def test_no_gateway(self):
        from app.api.projects import _find_gateway_node

        topo = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {}},
                {
                    "id": "net-1",
                    "type": "networkNode",
                    "data": {"subtype": "network"},
                },
            ]
        }
        assert _find_gateway_node(topo) is None

    def test_empty_topology(self):
        from app.api.projects import _find_gateway_node

        assert _find_gateway_node({}) is None
        assert _find_gateway_node({"nodes": []}) is None


class TestFindChangedKubevirtVms:
    """Tests for _find_changed_kubevirt_vms()."""

    def test_detects_changed_vm(self):
        from app.api.projects import _find_changed_kubevirt_vms

        current = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 4}},
                {"id": "vm-2", "type": "vmNode", "data": {"vcpus": 2}},
            ]
        }
        deployed = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 2}},
                {"id": "vm-2", "type": "vmNode", "data": {"vcpus": 2}},
            ]
        }
        changed = _find_changed_kubevirt_vms(current, deployed)
        assert "vm-1" in changed
        assert "vm-2" not in changed

    def test_no_changes(self):
        from app.api.projects import _find_changed_kubevirt_vms

        topo = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 2}},
            ]
        }
        assert _find_changed_kubevirt_vms(topo, topo) == []

    def test_added_vm_not_in_changed(self):
        from app.api.projects import _find_changed_kubevirt_vms

        current = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 2}},
                {"id": "vm-new", "type": "vmNode", "data": {"vcpus": 4}},
            ]
        }
        deployed = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"vcpus": 2}},
            ]
        }
        changed = _find_changed_kubevirt_vms(current, deployed)
        # vm-new is not in deployed, so not in changed list
        assert changed == []


class TestBuildKubevirtVmSpec:
    """Tests for _build_kubevirt_vm_spec()."""

    def test_basic_spec(self):
        from app.api.projects import _build_kubevirt_vm_spec

        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "label": "test-vm",
                        "vcpus": 4,
                        "ram": 8,
                        "nics": [{"ip": "10.0.0.5"}],
                        "firmware": "uefi",
                        "bootDevices": ["disk-1"],
                        "domainUuid": "abc-123",
                    },
                },
            ],
            "edges": [],
        }
        vm = {"name": "test-vm", "vcpus": 4, "ram_gb": 8, "firmware": "uefi"}
        with patch("app.services.deploy_topology._find_vm_disks", return_value=[]):
            spec = _build_kubevirt_vm_spec("vm-1", vm, topo)

        assert spec["cpus"] == 4
        assert spec["memory"] == 8 * 1024
        assert spec["firmware"] == "uefi"
        assert spec["smbiosUuid"] == "abc-123"
        assert spec["bootOrder"] == ["disk-1"]
        assert spec["disks"] == []

    def test_spec_with_blank_disk(self):
        from app.api.projects import _build_kubevirt_vm_spec

        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {"label": "vm", "vcpus": 2, "ram": 4, "nics": []},
                },
            ],
            "edges": [],
        }
        vm = {"name": "vm", "vcpus": 2, "ram_gb": 4}
        mock_disk = [{"node_id": "d1", "size": 50, "format": "qcow2"}]
        with patch(
            "app.services.deploy_topology._find_vm_disks", return_value=mock_disk
        ):
            spec = _build_kubevirt_vm_spec("vm-1", vm, topo)

        assert len(spec["disks"]) == 1
        assert spec["disks"][0]["sizeGb"] == 50
        assert spec["disks"][0]["blank"] is True


class TestFinalizeKubevirtReconfigure:
    """Tests for _finalize_kubevirt_reconfigure()."""

    def test_cleans_topology_and_sets_active(self):
        import copy

        from app.api.projects import _finalize_kubevirt_reconfigure

        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "label": "test",
                        "resolvedS3Path": "s3://bucket/key",
                        "presignedUrl": "https://example.com/signed",
                        "ciGeneratedUserData": "#cloud-config\n...",
                        "vcpus": 2,
                    },
                }
            ]
        }

        class FakeProject:
            deployed_topology = None
            topology = None
            state = "reconfiguring"
            deploy_error = "old error"

        class FakeSession:
            def commit(self):
                pass

        proj = FakeProject()
        notified = []

        def fake_notify(p_id, msg):
            notified.append(msg)

        _finalize_kubevirt_reconfigure(
            proj, FakeSession(), "p-123", topo, copy, fake_notify
        )

        assert proj.state == "active"
        assert proj.deploy_error is None
        # Sensitive fields should be stripped from deployed topology
        node_data = proj.deployed_topology["nodes"][0]["data"]
        assert "resolvedS3Path" not in node_data
        assert "presignedUrl" not in node_data
        assert "ciGeneratedUserData" not in node_data
        # Non-sensitive fields preserved
        assert node_data["vcpus"] == 2
        # Notification sent
        assert len(notified) == 1
        assert notified[0]["type"] == "project-state"
        assert notified[0]["state"] == "active"


# ===================================================================
# _do_reconfigure_bg -- background reconfigure function
# ===================================================================


class TestDoReconfigureBg:
    """Tests for _do_reconfigure_bg() called directly with mocked deps."""

    def _setup_project_and_host(self, state="active", host_type="shared"):
        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="running",
            host_type=host_type,
            ip_address="10.0.0.1",
            private_key="fake-ssh-key",
            agent_status="connected",
            agent_token="fake-token",
        )
        db.add(host)
        db.flush()
        topo = {
            "nodes": [
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "label": "test-vm",
                        "vcpus": 2,
                        "ram": 4,
                        "nics": [{"ip": "192.168.1.10"}],
                        "ciCloudUserPassword": "pass",
                    },
                },
                {
                    "id": "net-1",
                    "type": "networkNode",
                    "data": {"subtype": "network", "cidr": "192.168.1.0/24"},
                },
            ],
            "edges": [{"source": "net-1", "target": "vm-1"}],
        }
        p = Project(
            name="Reconfig BG Test",
            owner_id=USER_ID,
            state=state,
            host_id=host.id,
            topology=topo,
            deployed_topology=topo,
            vni_map={"net-1": 100},
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        pid = p.id
        hid = host.id
        db.close()
        return pid, hid

    def test_reconfigure_bg_success(self):
        from app.api.projects import _do_reconfigure_bg

        pid, hid = self._setup_project_and_host()

        no_diff = {
            "added_vms": [],
            "removed_vms": [],
            "changed_vms": [],
            "added_networks": [],
            "removed_networks": [],
            "has_changes": False,
        }

        with patch("app.api.projects.diff_topologies", return_value=no_diff), patch(
            "app.api.projects._setup_networks_via_troshkad", return_value=True
        ), patch("app.api.projects._sync_eips_for_reconfigure"), patch(
            "app.services.deploy_service._set_deploy_progress"
        ), patch(
            "app.services.deploy_service._delete_deploy_progress"
        ), patch(
            "app.api.projects.notify_project"
        ), patch(
            "app.api.projects.cache_library_images"
        ), patch(
            "app.api.projects._extract_vms", return_value=[]
        ), patch(
            "app.api.projects._setup_pxe_via_troshkad"
        ), patch(
            "app.api.projects._finalize_reconfigure"
        ) as mock_final:
            # Make _finalize_reconfigure set project to active
            def finalize_side_effect(s, proj, h, p_id, current, deployed, errors):
                proj.state = "active"
                proj.deploy_error = None
                s.commit()

            mock_final.side_effect = finalize_side_effect
            _do_reconfigure_bg(pid, hid, [])

        # Verify project ended up in active state
        db = TestSession()
        proj = db.query(Project).filter_by(id=pid).first()
        assert proj.state == "active"
        db.close()

    def test_reconfigure_bg_network_failure(self):
        from app.api.projects import _do_reconfigure_bg

        pid, hid = self._setup_project_and_host()

        no_diff = {
            "added_vms": [],
            "removed_vms": [],
            "changed_vms": [],
            "added_networks": [],
            "removed_networks": [],
            "has_changes": False,
        }

        with patch("app.api.projects.diff_topologies", return_value=no_diff), patch(
            "app.api.projects._setup_networks_via_troshkad",
            return_value="VXLAN setup failed",
        ), patch("app.api.projects._sync_eips_for_reconfigure"), patch(
            "app.services.deploy_service._set_deploy_progress"
        ), patch(
            "app.services.deploy_service._delete_deploy_progress"
        ):
            _do_reconfigure_bg(pid, hid, [])

        db = TestSession()
        proj = db.query(Project).filter_by(id=pid).first()
        assert proj.state == "error"
        assert "Network setup failed" in proj.deploy_error
        db.close()

    def test_reconfigure_bg_kubevirt_delegates(self):
        from app.api.projects import _do_reconfigure_bg

        pid, hid = self._setup_project_and_host(host_type="kubevirt-cluster")

        with patch("app.api.projects._do_reconfigure_kubevirt") as mock_kv:
            _do_reconfigure_bg(pid, hid, [])

        mock_kv.assert_called_once()
        args = mock_kv.call_args[0]
        assert args[0] == pid
        assert args[1] == hid
