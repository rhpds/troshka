"""Recert Status & Log timer resets at ops-pod start, not project deploy start."""

import datetime
from unittest.mock import MagicMock, patch

from app.services.deploy_service import (
    _stamp_recert_install_started,
    resolve_ocp_install_started_at,
)


def _pattern_cluster_topo(cluster_id="destination"):
    vm_id = "vm-aaaa-bbbb-cccc"
    disk_id = "disk-aaaa-bbbb-cccc"
    return {
        "clusters": [{"id": cluster_id, "name": cluster_id, "type": "sno"}],
        "nodes": [
            {
                "id": vm_id,
                "type": "vmNode",
                "data": {
                    "name": "cp-0",
                    "os": "rhcos",
                    "clusterId": cluster_id,
                    "diskControllers": [{"id": "dp-0", "bus": "virtio"}],
                },
            },
            {
                "id": disk_id,
                "type": "storageNode",
                "data": {
                    "size": 120,
                    "format": "qcow2",
                    "source": "pattern",
                    "patternId": "pat-1",
                    "patternDiskId": "pd-1",
                },
            },
        ],
        "edges": [
            {
                "source": vm_id,
                "target": disk_id,
                "sourceHandle": "dp-0",
                "targetHandle": "storage-in",
            }
        ],
    }


def test_resolve_prefers_cluster_stamp():
    assert (
        resolve_ocp_install_started_at(
            cluster_install_started_at=1700001000,
            cluster_log="[c] Waiting for cluster installation to complete (recert)",
            ocp_monitor_started_at=datetime.datetime(
                2023, 11, 14, 22, 16, 40, tzinfo=datetime.UTC
            ),
            deploy_started_at=1700000000.0,
        )
        == 1700001000
    )


def test_resolve_recert_falls_back_to_monitor_start():
    monitor = datetime.datetime(2023, 11, 14, 22, 16, 40, tzinfo=datetime.UTC)
    assert resolve_ocp_install_started_at(
        cluster_install_started_at=None,
        cluster_log="[destination] Waiting for cluster installation to complete (recert)",
        ocp_monitor_started_at=monitor,
        deploy_started_at=1700000000.0,
    ) == int(monitor.timestamp())


def test_resolve_fresh_install_uses_deploy_start():
    monitor = datetime.datetime(2023, 11, 14, 22, 16, 40, tzinfo=datetime.UTC)
    assert (
        resolve_ocp_install_started_at(
            cluster_install_started_at=None,
            cluster_log="[c] starting agent-based install",
            ocp_monitor_started_at=monitor,
            deploy_started_at=1700000000.0,
        )
        == 1700000000
    )


def test_stamp_recert_sets_install_started_at():
    topo = _pattern_cluster_topo()
    project = MagicMock()
    project.deployed_topology = topo
    project.topology = topo
    clusters = list(topo["clusters"])

    with patch("app.services.deploy_service._time.time", return_value=1700000900):
        assert _stamp_recert_install_started(project, clusters) is True

    assert topo["clusters"][0]["ocpInstallStartedAt"] == 1700000900
    assert topo["clusters"][0]["ocpInstallStatus"] == "monitoring"


def test_stamp_recert_does_not_overwrite_existing():
    topo = _pattern_cluster_topo()
    topo["clusters"][0]["ocpInstallStartedAt"] = 1700000500
    project = MagicMock()
    project.deployed_topology = topo
    project.topology = None
    clusters = list(topo["clusters"])

    with patch("app.services.deploy_service._time.time", return_value=1700000900):
        assert _stamp_recert_install_started(project, clusters) is False

    assert topo["clusters"][0]["ocpInstallStartedAt"] == 1700000500


def test_stamp_skips_fresh_install_clusters():
    topo = {
        "clusters": [{"id": "fresh", "name": "fresh", "type": "sno"}],
        "nodes": [
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "os": "rhcos",
                    "clusterId": "fresh",
                    "diskControllers": [{"id": "dp-0", "bus": "virtio"}],
                },
            },
            {
                "id": "disk-1",
                "type": "storageNode",
                "data": {"size": 120, "format": "qcow2", "source": "blank"},
            },
        ],
        "edges": [
            {
                "source": "vm-1",
                "target": "disk-1",
                "sourceHandle": "dp-0",
                "targetHandle": "storage-in",
            }
        ],
    }
    project = MagicMock()
    project.deployed_topology = topo
    project.topology = None

    assert _stamp_recert_install_started(project, topo["clusters"]) is False
    assert "ocpInstallStartedAt" not in topo["clusters"][0]
