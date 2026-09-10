"""Tests for recert kubeconfig-delivery helpers in deploy_service."""

from app.services.deploy_service import _recert_cp_member_vm_id


def _topo():
    return {
        "nodes": [
            {
                "id": "cp-node-1",
                "type": "vmNode",
                "data": {
                    "id": "cp-node-1",
                    "os": "rhcos",
                    "clusterId": "c1",
                    "clusterRole": "master",
                },
            },
            {
                "id": "worker-1",
                "type": "vmNode",
                "data": {
                    "id": "worker-1",
                    "os": "rhcos",
                    "clusterId": "c1",
                    "clusterRole": "worker",
                },
            },
        ]
    }


def test_picks_control_plane_member_not_worker():
    assert _recert_cp_member_vm_id(_topo(), {"id": "c1", "name": "c1"}) == "cp-node-1"


def test_returns_empty_when_no_rhcos_cp_member():
    topo = {
        "nodes": [
            {
                "id": "w",
                "type": "vmNode",
                "data": {
                    "id": "w",
                    "os": "rhcos",
                    "clusterId": "c1",
                    "clusterRole": "worker",
                },
            }
        ]
    }
    assert _recert_cp_member_vm_id(topo, {"id": "c1", "name": "c1"}) == ""


def test_pull_recert_kubeconfig_retries_until_ready():
    """lb-ext isn't on disk the instant the API answers /healthz, so pull_file
    is retried until it returns a non-empty kubeconfig (not one-shot)."""
    import time
    from unittest.mock import MagicMock, patch

    from app.services.deploy_service import _pull_recert_kubeconfig

    driver = MagicMock()
    driver.pull_file.side_effect = [b"", b"LB-EXT-KUBECONFIG"]  # not-ready, then ready
    logs = []
    with patch("time.sleep"):
        kc = _pull_recert_kubeconfig(
            driver,
            object(),
            "pid12345",
            "vmid",
            object(),
            time.time() + 300,
            logs.append,
        )
    assert kc == b"LB-EXT-KUBECONFIG"
    assert driver.pull_file.call_count == 2


def test_pull_recert_kubeconfig_survives_transient_exception():
    import time
    from unittest.mock import MagicMock, patch

    from app.services.deploy_service import _pull_recert_kubeconfig

    driver = MagicMock()
    driver.pull_file.side_effect = [RuntimeError("agent busy"), b"OK"]
    with patch("time.sleep"):
        kc = _pull_recert_kubeconfig(
            driver,
            object(),
            "pid12345",
            "vmid",
            object(),
            time.time() + 300,
            lambda m: None,
        )
    assert kc == b"OK"


def test_pull_recert_kubeconfig_gives_up_at_deadline():
    import time
    from unittest.mock import MagicMock, patch

    from app.services.deploy_service import _pull_recert_kubeconfig

    driver = MagicMock()
    driver.pull_file.return_value = b""  # never ready
    with patch("time.sleep"):
        kc = _pull_recert_kubeconfig(
            driver,
            object(),
            "pid12345",
            "vmid",
            object(),
            time.time() - 1,
            lambda m: None,
        )
    assert kc is None
