"""Tests for OCP rebuild-vs-recert helpers used by project redeploy."""

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.project import Project
from app.models.user import User
from app.services.redeploy_ocp import (
    all_ocp_clusters_ready,
    apply_ocp_rebuild_to_project,
    clear_topology_ocp_for_rebuild,
    topology_has_ocp_clusters,
)
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(
    email=f"redeploy-ocp-{uuid.uuid4().hex[:8]}@example.com",
    display_name="RedeployOcp",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
HEADERS = {
    "Authorization": f"Bearer {create_jwt(user_id=_user.id, email=_user.email, role=_user.role)}"
}
_OWNER_ID = _user.id
_db.close()


def _ocp_topo(*, status="ready", with_kubeconfig=True):
    vm_data = {"vcpus": 2, "ram": 4, "name": "cp0", "clusterId": "source"}
    if with_kubeconfig:
        vm_data["ocpKubeconfig"] = "kc"
        vm_data["recertEnabled"] = True
    return {
        "clusters": [{"id": "source", "name": "source", "ocpInstallStatus": status}],
        "nodes": [
            {"id": "vm1", "type": "vmNode", "data": vm_data},
            {
                "id": "disk1",
                "type": "storageNode",
                "data": {"name": "disk1", "size": 100},
            },
        ],
        "edges": [],
    }


def _create_project(*, topology, state="active"):
    db = TestSession()
    proj = Project(
        id=str(uuid.uuid4()),
        name=f"redeploy-ocp-{uuid.uuid4().hex[:6]}",
        owner_id=_OWNER_ID,
        state=state,
        topology=topology,
        deployed_topology=topology,
        ocp_status="ready" if topology.get("clusters") else None,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    pid = proj.id
    db.close()
    return pid


def test_topology_has_ocp_clusters_from_clusters_list():
    assert topology_has_ocp_clusters({"clusters": [{"id": "source"}]}) is True
    assert topology_has_ocp_clusters({"clusters": []}) is False
    assert topology_has_ocp_clusters(None) is False
    assert topology_has_ocp_clusters({}) is False


def test_topology_has_ocp_clusters_from_cluster_nodes():
    assert (
        topology_has_ocp_clusters(
            {"nodes": [{"type": "clusterNode", "data": {"name": "source"}}]}
        )
        is True
    )
    assert (
        topology_has_ocp_clusters({"nodes": [{"type": "vmNode", "data": {}}]}) is False
    )


def test_all_ocp_clusters_ready_requires_every_cluster():
    ready = {
        "clusters": [
            {"id": "a", "ocpInstallStatus": "ready"},
            {"id": "b", "ocpInstallStatus": "ready"},
        ]
    }
    assert all_ocp_clusters_ready(ready) is True

    mixed = {
        "clusters": [
            {"id": "a", "ocpInstallStatus": "ready"},
            {"id": "b", "ocpInstallStatus": "error"},
        ]
    }
    assert all_ocp_clusters_ready(mixed) is False
    assert all_ocp_clusters_ready({"clusters": []}) is False
    assert all_ocp_clusters_ready(None) is False


def test_clear_topology_ocp_for_rebuild_strips_markers():
    topo = {
        "clusters": [
            {
                "id": "source",
                "ocpInstallStatus": "ready",
                "ocpInstallElapsed": 99,
                "ocpInstallStartedAt": 1,
                "recert": True,
            }
        ],
        "nodes": [
            {
                "type": "vmNode",
                "data": {
                    "name": "source-cp-0",
                    "clusterId": "source",
                    "ocpKubeconfig": "kc",
                    "ocpKubeadminPassword": "pw",
                    "recertEnabled": True,
                    "guestfishCommands": ["x"],
                },
            },
            {
                "type": "storageNode",
                "data": {
                    "name": "disk0",
                    "source": "pattern",
                    "patternId": "pat-1",
                    "patternDiskId": "pd-1",
                },
            },
        ],
    }
    clear_topology_ocp_for_rebuild(topo)
    assert "ocpInstallStatus" not in topo["clusters"][0]
    assert "ocpInstallElapsed" not in topo["clusters"][0]
    assert "ocpInstallStartedAt" not in topo["clusters"][0]
    assert topo["clusters"][0]["recert"] is True  # template flag; not the path signal
    vm = topo["nodes"][0]["data"]
    assert "ocpKubeconfig" not in vm
    assert "ocpKubeadminPassword" not in vm
    assert "recertEnabled" not in vm
    assert "guestfishCommands" not in vm
    disk = topo["nodes"][1]["data"]
    assert "source" not in disk
    assert "patternId" not in disk
    assert "patternDiskId" not in disk


def test_apply_ocp_rebuild_to_project_clears_both_topologies_and_project_fields():
    class FakeProject:
        topology = {
            "clusters": [{"id": "c1", "ocpInstallStatus": "ready"}],
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"ocpKubeconfig": "kc", "recertEnabled": True},
                }
            ],
        }
        deployed_topology = {
            "clusters": [{"id": "c1", "ocpInstallStatus": "ready"}],
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"ocpKubeconfig": "kc2", "recertEnabled": True},
                }
            ],
        }
        ocp_status = "ready"
        ocp_status_detail = "ok"
        ocp_install_elapsed = 100
        ocp_monitor_started_at = object()
        ocp_control_plane_usable_at = object()
        ocp_control_plane_usable_elapsed = 50

    proj = FakeProject()
    apply_ocp_rebuild_to_project(proj)
    assert "ocpKubeconfig" not in proj.topology["nodes"][0]["data"]
    assert "ocpKubeconfig" not in proj.deployed_topology["nodes"][0]["data"]
    assert "ocpInstallStatus" not in proj.topology["clusters"][0]
    assert proj.ocp_status is None
    assert proj.ocp_status_detail is None
    assert proj.ocp_install_elapsed is None
    assert proj.ocp_monitor_started_at is None
    assert proj.ocp_control_plane_usable_at is None
    assert proj.ocp_control_plane_usable_elapsed is None


def test_redeploy_ocp_requires_mode():
    pid = _create_project(topology=_ocp_topo())
    resp = client.post(f"/api/v1/projects/{pid}/redeploy", headers=HEADERS)
    assert resp.status_code == 400
    assert "ocp_mode" in resp.json()["detail"].lower()


def test_redeploy_ocp_rejects_invalid_mode():
    pid = _create_project(topology=_ocp_topo())
    resp = client.post(
        f"/api/v1/projects/{pid}/redeploy",
        headers=HEADERS,
        json={"ocp_mode": "nope"},
    )
    assert resp.status_code == 400


def test_redeploy_ocp_recert_requires_all_ready():
    pid = _create_project(topology=_ocp_topo(status="error"))
    resp = client.post(
        f"/api/v1/projects/{pid}/redeploy",
        headers=HEADERS,
        json={"ocp_mode": "recert"},
    )
    assert resp.status_code == 409
    assert "ready" in resp.json()["detail"].lower()


@patch("app.core.redis.enqueue_job")
@patch("app.services.deploy_service._mark_deploy_cancelled")
def test_redeploy_ocp_rebuild_clears_markers(mock_cancel, mock_enqueue):
    pid = _create_project(topology=_ocp_topo())
    resp = client.post(
        f"/api/v1/projects/{pid}/redeploy",
        headers=HEADERS,
        json={"ocp_mode": "rebuild"},
    )
    assert resp.status_code == 200
    mock_enqueue.assert_called_once()
    args = mock_enqueue.call_args.args
    assert args[4] == "rebuild"

    db = TestSession()
    proj = db.query(Project).filter_by(id=pid).first()
    assert "ocpKubeconfig" not in (proj.topology["nodes"][0]["data"])
    assert "ocpInstallStatus" not in proj.topology["clusters"][0]
    assert proj.ocp_status is None
    assert proj.state == "deploying"
    db.close()


@patch("app.core.redis.enqueue_job")
@patch("app.services.deploy_service._mark_deploy_cancelled")
def test_redeploy_ocp_recert_keeps_markers_when_ready(mock_cancel, mock_enqueue):
    pid = _create_project(topology=_ocp_topo(status="ready"))
    resp = client.post(
        f"/api/v1/projects/{pid}/redeploy",
        headers=HEADERS,
        json={"ocp_mode": "recert"},
    )
    assert resp.status_code == 200
    args = mock_enqueue.call_args.args
    assert args[4] == "recert"
    assert args[2] is None  # destroy_ctx skipped to preserve disks
    db = TestSession()
    proj = db.query(Project).filter_by(id=pid).first()
    assert proj.topology["nodes"][0]["data"].get("ocpKubeconfig") == "kc"
    db.close()


@patch("app.core.redis.enqueue_job")
@patch("app.services.deploy_service._mark_deploy_cancelled")
def test_redeploy_non_ocp_unchanged(mock_cancel, mock_enqueue):
    pid = _create_project(
        topology={
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"vcpus": 2, "ram": 4, "name": "vm"},
                }
            ],
            "edges": [],
        }
    )
    resp = client.post(f"/api/v1/projects/{pid}/redeploy", headers=HEADERS)
    assert resp.status_code == 200
    mock_enqueue.assert_called_once()
