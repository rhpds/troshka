"""Ops-pod kubeconfig harvest: early snarf at install-complete + merged terminal inject."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

from app.services.deploy_service import _monitor_ops_pod_install, _store_ops_pod_creds
from tests.conftest import sample_kubeadmin_password, sample_kubeconfig_yaml

PROJECT_ID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
SVC = "app.services.deploy_service"


def _make_host():
    h = MagicMock()
    h.id = "host-0001"
    h.ip_address = "10.0.0.1"
    h.host_type = "kubevirt-cluster"
    h.provider_id = "provider-0001"
    return h


def _make_project(topology):
    p = MagicMock()
    p.id = PROJECT_ID
    p.topology = topology
    p.deployed_topology = copy.deepcopy(topology)
    return p


@patch(f"{SVC}._maybe_heal_stuck_ops_pod", return_value={})
@patch(f"{SVC}._store_ops_pod_creds", return_value={"source"})
@patch(f"{SVC}._ocp_update_status")
@patch(f"{SVC}._publish_ops_pod_progress")
@patch(f"{SVC}._ops_pod_running", return_value=True)
@patch(f"{SVC}._read_ops_pod_cluster_logs")
@patch(f"{SVC}._is_deploy_cancelled", return_value=False)
@patch(f"{SVC}.cache_ops_pod_logs", side_effect=lambda _pid, logs: logs)
def test_monitor_harvests_creds_at_install_complete_before_workers(
    _cache, _cancel, mock_logs, _running, _pub, _status, mock_store, _heal
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
                    "ocpKubeconfig": sample_kubeconfig_yaml(),
                    "ocpKubeadminPassword": sample_kubeadmin_password(),
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
        "/workdir/source/auth/kubeadmin-password": sample_kubeadmin_password(),
        "/workdir/source/auth/kubeconfig": sample_kubeconfig_yaml(
            "https://api.source:6443"
        ),
    }.get(path, "")

    _store_ops_pod_creds(_make_host(), PROJECT_ID, [{"id": "source"}], "/workdir")

    mock_inject.assert_called_once()
    injected_creds = mock_inject.call_args[0][3]
    assert set(injected_creds) == {"destination", "source"}
    assert injected_creds["destination"][1] == sample_kubeconfig_yaml()
    assert injected_creds["source"][1] == sample_kubeconfig_yaml(
        "https://api.source:6443"
    )


@patch(f"{SVC}._inject_cluster_kubeconfigs")
@patch(f"{SVC}._ops_pod_cat")
@patch("app.core.database.SessionLocal")
def test_store_ops_pod_creds_uses_deployed_when_editable_stripped(
    mock_session_local, mock_cat, mock_inject
):
    """Canvas autosave strips ocpKubeconfig from editable topology.

    A later cluster harvest must still merge earlier clusters from
    deployed_topology — otherwise /showroom/kube/config is overwritten with a
    single-cluster merge (CCLM source-only terminal).
    """
    editable = {
        "clusters": [
            {"id": "source", "name": "source", "apiVip": "10.0.0.10"},
            {"id": "destination", "name": "destination", "apiVip": "10.0.0.20"},
        ],
        "nodes": [
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "source",
                    "clusterRole": "control-plane",
                },
            },
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "destination",
                    "clusterRole": "control-plane",
                },
            },
        ],
    }
    deployed = copy.deepcopy(editable)
    deployed["nodes"][0]["data"]["ocpKubeconfig"] = sample_kubeconfig_yaml(
        "https://api.source:6443"
    )
    deployed["nodes"][0]["data"]["ocpKubeadminPassword"] = sample_kubeadmin_password()

    project = MagicMock()
    project.id = PROJECT_ID
    project.topology = editable
    project.deployed_topology = deployed
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = project
    mock_session_local.return_value = session
    dest_kc = sample_kubeconfig_yaml("https://api.destination:6443")
    mock_cat.side_effect = lambda _h, _pid, _ctr, path: {
        "/workdir/destination/auth/kubeadmin-password": sample_kubeadmin_password(),
        "/workdir/destination/auth/kubeconfig": dest_kc,
    }.get(path, "")

    _store_ops_pod_creds(_make_host(), PROJECT_ID, [{"id": "destination"}], "/workdir")

    mock_inject.assert_called_once()
    injected_creds = mock_inject.call_args[0][3]
    assert set(injected_creds) == {"source", "destination"}
    assert injected_creds["source"][1] == sample_kubeconfig_yaml(
        "https://api.source:6443"
    )
    assert injected_creds["destination"][1] == dest_kc
