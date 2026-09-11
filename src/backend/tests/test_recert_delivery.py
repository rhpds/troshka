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


def test_delivery_skips_snapshot_pull_when_captured_kubeconfig_works():
    """Lazy delivery: if the captured kubeconfig already authenticates (ocpvirt),
    skip the expensive snapshot+guestfish lb-ext pull entirely (and its extra
    'admin kubeconfig ...' log lines) — the block will use the captured one."""
    from unittest.mock import MagicMock, patch

    import app.services.deploy_service as ds

    with patch.object(ds, "_wait_nested_apiserver_up", return_value=True), patch.object(
        ds, "_captured_kubeconfig_works", return_value=True
    ) as cap, patch.object(ds, "_pull_recert_kubeconfig") as pull, patch.object(
        ds, "_ops_pod_log_line"
    ), patch.object(
        ds, "_recert_cp_member_vm_id", return_value="cp-node-1"
    ):
        ds._deliver_one_recert_kubeconfig(
            MagicMock(),
            "prov",
            MagicMock(),
            "proj",
            {"id": "c1", "name": "c1"},
            _topo(),
            "ctr",
            "/workdir",
            10**12,
        )
        cap.assert_called_once()
        pull.assert_not_called()  # snapshot pull skipped when captured works


def test_delivery_pulls_when_captured_kubeconfig_fails():
    """KubeVirt: the captured kubeconfig's CAs roll, so it fails to authenticate —
    the snapshot lb-ext pull must still run and deliver."""
    from unittest.mock import MagicMock, patch

    import app.services.deploy_service as ds

    with patch.object(ds, "_wait_nested_apiserver_up", return_value=True), patch.object(
        ds, "_captured_kubeconfig_works", return_value=False
    ), patch.object(
        ds, "_pull_recert_kubeconfig", return_value="KC"
    ) as pull, patch.object(
        ds, "_ops_pod_write_file", return_value=True
    ) as write, patch.object(
        ds, "_ops_pod_log_line"
    ), patch.object(
        ds, "_recert_cp_member_vm_id", return_value="cp-node-1"
    ):
        ds._deliver_one_recert_kubeconfig(
            MagicMock(),
            "prov",
            MagicMock(),
            "proj",
            {"id": "c1", "name": "c1"},
            _topo(),
            "ctr",
            "/workdir",
            10**12,
        )
        pull.assert_called_once()  # still pulls when captured fails
        write.assert_called_once()
