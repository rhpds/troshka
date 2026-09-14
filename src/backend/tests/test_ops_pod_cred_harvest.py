"""Ops-pod kubeconfig harvest: early snarf at install-complete + merged terminal inject."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

from app.services.deploy_service import _monitor_ops_pod_install, _store_ops_pod_creds

PROJECT_ID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
SVC = "app.services.deploy_service"


def _make_host():
    h = MagicMock()
    h.id = "host-0001"
    h.ip_address = "10.0.0.1"
    h.host_type = "kubevirt-cluster"
    return h


def _make_project(topology):
    p = MagicMock()
    p.id = PROJECT_ID
    p.topology = topology
    p.deployed_topology = copy.deepcopy(topology)
    return p


@patch(f"{SVC}._store_ops_pod_creds")
@patch(f"{SVC}._ocp_update_status")
@patch(f"{SVC}._publish_ops_pod_progress")
@patch(f"{SVC}._ops_pod_running", return_value=True)
@patch(f"{SVC}._read_ops_pod_cluster_logs")
@patch(f"{SVC}._is_deploy_cancelled", return_value=False)
@patch(f"{SVC}.cache_ops_pod_logs", side_effect=lambda _pid, logs: logs)
def test_monitor_harvests_creds_at_install_complete_before_workers(
    _cache, _cancel, mock_logs, _running, _pub, _status, mock_store
):
    """SNO + deferred workers: harvest when install completes, not after worker join."""
    source = {
        "id": "source",
        "name": "source",
        "type": "sno",
        "workers": 2,
    }
    joining = (
        "[source] install complete\n"
        "[source] control-plane-usable\n"
        "[source] joining 2 deferred worker(s)\n"
        "[source] node-image create for source-worker-0\n"
    )
    mock_logs.return_value = {"source": joining}

    result = _monitor_ops_pod_install(
        PROJECT_ID, _make_host(), [source], poll_interval=0, timeout=5
    )

    assert result == "timeout"
    mock_store.assert_called_once()
    assert mock_store.call_args[0][2] == [source]


@patch(f"{SVC}._inject_cluster_kubeconfigs")
@patch(f"{SVC}._ops_pod_cat")
@patch("app.core.database.SessionLocal")
def test_store_ops_pod_creds_injects_all_harvested_clusters(
    mock_session_local, mock_cat, mock_inject
):
    """Each harvest merges every stored cluster into the terminal kubeconfig."""
    topo = {
        "clusters": [
            {"id": "destination", "name": "destination"},
            {"id": "source", "name": "source"},
        ],
        "nodes": [
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "destination",
                    "clusterRole": "control-plane",
                    "ocpKubeconfig": "dest-kc",
                    "ocpKubeadminPassword": "dest-pw",
                },
            },
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "source",
                    "clusterRole": "control-plane",
                },
            },
        ],
    }
    project = _make_project(copy.deepcopy(topo))
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = project
    mock_session_local.return_value = session
    mock_cat.side_effect = lambda _h, _pid, _ctr, path: {
        "/workdir/source/auth/kubeadmin-password": "src-pw",
        "/workdir/source/auth/kubeconfig": "src-kc",
    }.get(path, "")

    _store_ops_pod_creds(_make_host(), PROJECT_ID, [{"id": "source"}], "/workdir")

    mock_inject.assert_called_once()
    injected_creds = mock_inject.call_args[0][3]
    assert set(injected_creds) == {"destination", "source"}
    assert injected_creds["destination"][1] == "dest-kc"
    assert injected_creds["source"][1] == "src-kc"
