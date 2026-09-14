"""Restart failed ops-pod OCP install (Phase 1 pre-boot + Phase 2 post-boot)."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.deploy_service import (
    _clusters_for_ops_pod_restart,
    validate_restart_ocp_cluster_install,
)
from app.services.ocp.ops_pod_install import cluster_install_post_boot


@pytest.mark.parametrize(
    "log,expected",
    [
        ("", False),
        ("level=fatal create image failed", False),
        ("Serving via HTTP on port 8080", True),
        ("InsertMedia and ForceRestart", True),
        ("reached installation stage waiting", True),
    ],
)
def test_cluster_install_post_boot(log, expected):
    assert cluster_install_post_boot(log) is expected


def test_validate_restart_rejects_complete():
    project = MagicMock()
    project.state = "active"
    project.id = "proj-1"
    project.deployed_topology = {
        "clusters": [
            {"id": "ocp-1", "ocpInstallStatus": "ready", "installOnDeploy": True}
        ]
    }
    host = MagicMock()
    topology = project.deployed_topology
    with patch(
        "app.services.template_loader.ocp_install_via", return_value="pod"
    ), patch(
        "app.services.deploy_service._ops_pod_cluster_complete", return_value=False
    ):
        err = validate_restart_ocp_cluster_install(project, host, topology, "ocp-1")
    assert err == "Cluster install already completed"


def test_validate_restart_rejects_recert():
    project = MagicMock()
    project.state = "active"
    project.id = "proj-1"
    project.deployed_topology = {
        "clusters": [
            {"id": "ocp-1", "ocpInstallStatus": "error", "installOnDeploy": True}
        ]
    }
    host = MagicMock()
    topology = project.deployed_topology
    with patch(
        "app.services.template_loader.ocp_install_via", return_value="pod"
    ), patch(
        "app.services.deploy_service._cluster_install_log_text",
        return_value="[ocp-1] Waiting (recert)",
    ), patch(
        "app.services.deploy_service._ops_pod_cluster_complete", return_value=False
    ):
        err = validate_restart_ocp_cluster_install(project, host, topology, "ocp-1")
    assert err == "Cannot restart a recert install"


def test_validate_restart_allows_failed():
    project = MagicMock()
    project.state = "active"
    project.id = "proj-1"
    project.deployed_topology = {
        "clusters": [
            {"id": "ocp-1", "ocpInstallStatus": "error", "installOnDeploy": True}
        ]
    }
    host = MagicMock()
    topology = project.deployed_topology
    with patch(
        "app.services.template_loader.ocp_install_via", return_value="pod"
    ), patch(
        "app.services.deploy_service._cluster_install_log_text",
        return_value="level=fatal unauthorized",
    ), patch(
        "app.services.deploy_service._ops_pod_cluster_complete", return_value=False
    ):
        err = validate_restart_ocp_cluster_install(project, host, topology, "ocp-1")
    assert err is None


def test_clusters_for_ops_pod_restart_keeps_inflight_siblings():
    topology = {
        "clusters": [
            {"id": "ocp-a", "installOnDeploy": True},
            {"id": "ocp-b", "installOnDeploy": True},
        ]
    }
    deployed = topology
    with patch(
        "app.services.deploy_service._ops_pod_cluster_complete",
        return_value=False,
    ):
        expanded = _clusters_for_ops_pod_restart(topology, deployed, "ocp-a", "proj-1")
    keys = {c["id"] for c in expanded}
    assert keys == {"ocp-a", "ocp-b"}


def test_clusters_for_ops_pod_restart_excludes_complete_siblings():
    topology = {
        "clusters": [
            {"id": "ocp-a", "installOnDeploy": True},
            {"id": "ocp-b", "installOnDeploy": True},
        ]
    }
    deployed = topology
    with patch(
        "app.services.deploy_service._ops_pod_cluster_complete",
        side_effect=lambda _pid, c: c.get("id") == "ocp-b",
    ):
        expanded = _clusters_for_ops_pod_restart(topology, deployed, "ocp-a", "proj-1")
    keys = {c["id"] for c in expanded}
    assert keys == {"ocp-a"}
