"""Unit tests for kubevirt helpers extracted for Sonar S3776.

All kubernetes / network I/O is mocked — no real cluster calls.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("TROSHKA_DATABASE__URL", "sqlite:///./test.db")

from app.services.providers.kubevirt import (
    _apply_mtu_map,
    _available_unclaimed_rbd_entry,
    _build_project_s3_cr_config,
    _build_troshka_project_cr,
    _central_s3_cr_config,
    _cleanup_capture_temp_pvcs,
    _cleanup_volume_snapshots,
    _clear_one_volume_snapshot_content,
    _delete_one_capture_temp_pvc,
    _ensure_project_namespace,
    _force_clear_rook_finalizers,
    _list_volume_snapshot_contents,
    _list_volume_snapshots,
    _node_allocatable_vcpus_ram_mb,
    _node_is_schedulable_worker,
    _obc_s3_dict,
    _orphan_pv_entry,
    _pv_capacity_storage,
    _query_cluster_capacity,
    _strip_namespaced_core_finalizers,
    _strip_pvc_finalizers,
    _strip_rook_crs,
    _troshka_orphan_ns_suffix,
    list_orphan_troshka_pvs,
    list_unclaimed_available_rbd_pvs,
)


def _api_exc(status: int):
    from kubernetes.client.exceptions import ApiException

    return ApiException(status=status)


class TestVolumeSnapshotHelpers:
    def test_list_snapshots_404_returns_none(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = _api_exc(404)
        assert _list_volume_snapshots(custom_api, "ns") is None

    def test_list_contents_403_returns_none(self):
        custom_api = MagicMock()
        custom_api.list_cluster_custom_object.side_effect = _api_exc(403)
        assert _list_volume_snapshot_contents(custom_api) is None

    def test_clear_content_ignores_404(self):
        custom_api = MagicMock()
        custom_api.patch_cluster_custom_object.side_effect = _api_exc(404)
        custom_api.delete_cluster_custom_object.side_effect = _api_exc(404)
        _clear_one_volume_snapshot_content(custom_api, "snap-c")

    @patch("app.services.providers.kubevirt._strip_namespaced_cr_finalizers")
    def test_cleanup_volume_snapshots_filters_ns(self, strip_fn):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "snap-a"}},
                {"metadata": {}},
            ]
        }
        custom_api.list_cluster_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "c-other"},
                    "spec": {"volumeSnapshotRef": {"namespace": "other"}},
                },
                {
                    "metadata": {"name": "c-mine"},
                    "spec": {"volumeSnapshotRef": {"namespace": "ns"}},
                },
            ]
        }
        _cleanup_volume_snapshots(custom_api, "ns")
        strip_fn.assert_called_once()
        custom_api.delete_cluster_custom_object.assert_called_once()
        assert (
            custom_api.delete_cluster_custom_object.call_args.kwargs["name"] == "c-mine"
        )


class TestCaptureTempPvcHelpers:
    def test_strip_pvc_finalizers_404(self):
        core_api = MagicMock()
        core_api.patch_namespaced_persistent_volume_claim.side_effect = _api_exc(404)
        _strip_pvc_finalizers(core_api, "ns", "export-x")

    def test_delete_skips_non_temp_prefix(self):
        core_api = MagicMock()
        pvc = SimpleNamespace(metadata=SimpleNamespace(name="root-disk", finalizers=[]))
        _delete_one_capture_temp_pvc(core_api, "ns", pvc)
        core_api.delete_namespaced_persistent_volume_claim.assert_not_called()

    def test_delete_temp_strips_and_deletes(self):
        core_api = MagicMock()
        pvc = SimpleNamespace(
            metadata=SimpleNamespace(name="export-abc", finalizers=["f1"])
        )
        _delete_one_capture_temp_pvc(core_api, "ns", pvc)
        core_api.patch_namespaced_persistent_volume_claim.assert_called_once()
        core_api.delete_namespaced_persistent_volume_claim.assert_called_once()

    def test_cleanup_list_exception(self):
        core_api = MagicMock()
        core_api.list_namespaced_persistent_volume_claim.side_effect = RuntimeError("x")
        _cleanup_capture_temp_pvcs(core_api, "ns")


class TestOrphanPvHelpers:
    def test_suffix_valid(self):
        assert _troshka_orphan_ns_suffix("troshka-abcdef12") == "abcdef12"

    def test_suffix_rejects_bad(self):
        assert _troshka_orphan_ns_suffix(None) is None
        assert _troshka_orphan_ns_suffix("other-abcdef12") is None
        assert _troshka_orphan_ns_suffix("troshka-ABCDEF12") is None
        assert _troshka_orphan_ns_suffix("troshka-abcd") is None

    @patch(
        "app.services.providers.kubevirt._pv_rbd_target",
        return_value={"pool": "p", "image": "i"},
    )
    def test_orphan_entry(self, _tgt):
        pv = SimpleNamespace(metadata=SimpleNamespace(name="pv-1"))
        claim = SimpleNamespace(name="claim-1", namespace="troshka-abcdef12")
        entry = _orphan_pv_entry(pv, "Released", claim, "troshka-abcdef12")
        assert entry["pv"] == "pv-1"
        assert entry["pool"] == "p"
        assert entry["claim"] == "claim-1"

    @patch("app.services.providers.kubevirt._pv_rbd_target", return_value=None)
    def test_list_orphan_filters_known(self, _tgt):
        core_api = MagicMock()
        claim = SimpleNamespace(name="c", namespace="troshka-abcdef12")
        pv = SimpleNamespace(
            metadata=SimpleNamespace(name="pv-1"),
            status=SimpleNamespace(phase="Released"),
            spec=SimpleNamespace(claim_ref=claim),
        )
        core_api.list_persistent_volume.return_value = SimpleNamespace(items=[pv])
        assert list_orphan_troshka_pvs(core_api, {"abcdef12"}) == []
        orphans = list_orphan_troshka_pvs(core_api, set())
        assert len(orphans) == 1
        assert orphans[0]["namespace"] == "troshka-abcdef12"

    def test_list_orphan_api_exception(self):
        core_api = MagicMock()
        core_api.list_persistent_volume.side_effect = _api_exc(500)
        assert list_orphan_troshka_pvs(core_api, set()) == []


class TestUnclaimedRbdHelpers:
    def test_capacity_dict(self):
        pv = SimpleNamespace(spec=SimpleNamespace(capacity={"storage": "10Gi"}))
        assert _pv_capacity_storage(pv) == "10Gi"

    def test_capacity_attr(self):
        pv = SimpleNamespace(
            spec=SimpleNamespace(capacity=SimpleNamespace(storage="5Gi"))
        )
        assert _pv_capacity_storage(pv) == "5Gi"

    @patch(
        "app.services.providers.kubevirt._pv_rbd_target",
        return_value={"pv": "pv-x", "pool": "p", "image": "img"},
    )
    def test_available_entry(self, _tgt):
        pv = SimpleNamespace(
            status=SimpleNamespace(phase="Available"),
            spec=SimpleNamespace(claim_ref=None, capacity={"storage": "1Gi"}),
        )
        entry = _available_unclaimed_rbd_entry(pv)
        assert entry["pv"] == "pv-x"
        assert entry["size"] == "1Gi"

    def test_skips_bound_claim(self):
        pv = SimpleNamespace(
            status=SimpleNamespace(phase="Available"),
            spec=SimpleNamespace(
                claim_ref=SimpleNamespace(namespace="ns"), capacity={}
            ),
        )
        assert _available_unclaimed_rbd_entry(pv) is None

    @patch(
        "app.services.providers.kubevirt._pv_rbd_target",
        return_value={"pv": "pv-x", "pool": "p", "image": "img"},
    )
    def test_list_unclaimed(self, _tgt):
        core_api = MagicMock()
        pv = SimpleNamespace(
            status=SimpleNamespace(phase="Available"),
            spec=SimpleNamespace(claim_ref=None, capacity={"storage": "1Gi"}),
        )
        core_api.list_persistent_volume.return_value = SimpleNamespace(items=[pv])
        found = list_unclaimed_available_rbd_pvs(core_api)
        assert len(found) == 1


class TestRookFinalizerHelpers:
    @patch("app.services.providers.kubevirt._strip_namespaced_cr_finalizers")
    def test_strip_rook_crs(self, strip_fn):
        custom_api = MagicMock()
        _strip_rook_crs(custom_api, "ns")
        assert strip_fn.call_count == 2

    def test_strip_core_finalizers(self):
        listed = SimpleNamespace(
            items=[
                SimpleNamespace(metadata=SimpleNamespace(name="a", finalizers=["f"])),
                SimpleNamespace(metadata=SimpleNamespace(name="b", finalizers=[])),
            ]
        )
        list_fn = MagicMock(return_value=listed)
        patch_fn = MagicMock()
        _strip_namespaced_core_finalizers(list_fn, patch_fn, "ns", "Secret")
        patch_fn.assert_called_once()
        assert patch_fn.call_args.kwargs["name"] == "a"

    @patch("app.services.providers.kubevirt._cleanup_project_persistent_volumes")
    @patch("app.services.providers.kubevirt._cleanup_capture_temp_pvcs")
    @patch("app.services.providers.kubevirt._cleanup_volume_snapshots")
    @patch("app.services.providers.kubevirt._strip_namespaced_core_finalizers")
    @patch("app.services.providers.kubevirt._strip_rook_crs")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_force_clear_rook_finalizers(
        self,
        get_clients,
        strip_rook,
        strip_core,
        cleanup_snaps,
        cleanup_pvcs,
        cleanup_pvs,
    ):
        custom_api, core_api = MagicMock(), MagicMock()
        get_clients.return_value = (custom_api, core_api, None)
        provider = MagicMock()
        provider.get_credentials.return_value = {}
        _force_clear_rook_finalizers(provider, "abcdef12-xxxx")
        strip_rook.assert_called_once()
        assert strip_core.call_count == 3
        cleanup_snaps.assert_called_once()
        cleanup_pvcs.assert_called_once()
        cleanup_pvs.assert_called_once()


class TestDeployHelpers:
    def test_ensure_namespace_already_exists(self):
        core_api = MagicMock()
        core_api.create_namespace.side_effect = Exception("AlreadyExists")
        _ensure_project_namespace(core_api, "troshka-abcdef12", "abcdef12-xxxx")

    def test_ensure_namespace_reraises(self):
        core_api = MagicMock()
        core_api.create_namespace.side_effect = Exception("Forbidden")
        with pytest.raises(Exception, match="Forbidden"):
            _ensure_project_namespace(core_api, "ns", "pid")

    def test_obc_s3_dict(self):
        d = _obc_s3_dict(
            {
                "access_key_id": "a",
                "secret_access_key": "s",
                "endpoint": "http://e",
                "bucket": "b",
            }
        )
        assert d["endpoint_url"] == "http://e"
        assert d["bucket"] == "b"

    def test_s3_cr_config_with_obc(self):
        cfg = _build_project_s3_cr_config(
            {"bucket": "b", "endpoint_url": "http://e", "region": "us-east-1"},
            {"bucket": "obc", "endpoint": "http://obc", "region": "us-east-1"},
        )
        assert cfg["bucket"] == "b"
        assert cfg["obcConfig"]["bucket"] == "obc"

    def test_apply_mtu_map(self):
        topo = {
            "nodes": [
                {"id": "n1", "type": "networkNode", "data": {}},
                {"id": "v1", "type": "vmNode", "data": {}},
            ]
        }
        _apply_mtu_map(topo, {"n1": 1400})
        assert topo["nodes"][0]["data"]["mtu"] == 1400
        assert "mtu" not in topo["nodes"][1]["data"]

    def test_central_s3_cr_config(self):
        cfg = _central_s3_cr_config(
            {
                "bucket": "c",
                "endpoint": "http://c",
                "region": "r",
                "access_key_id": "a",
                "secret_access_key": "s",
            }
        )
        assert cfg["credentialsSecret"] == "s3-central-credentials"
        assert cfg["endpoint"] == "http://c"

    def test_build_troshka_project_cr(self):
        cr = _build_troshka_project_cr(
            "abcdef12-xxxx",
            "troshka-abcdef12",
            {"nodes": []},
            {"bucket": "b"},
            {"bucket": "c", "endpoint_url": "http://c", "region": "r"},
            {
                "common_password": "pw",
                "registry_credentials": {"u": "p"},
                "exec_ssh_key": "ssh-rsa AAA",
            },
        )
        assert cr["metadata"]["name"] == "project-abcdef12"
        assert cr["spec"]["commonPassword"] == "pw"
        assert cr["spec"]["centralS3Config"]["bucket"] == "c"
        assert cr["spec"]["execSshKey"] == "ssh-rsa AAA"


class TestClusterCapacityHelpers:
    def test_schedulable_worker(self):
        node = SimpleNamespace(
            metadata=SimpleNamespace(labels={"node-role.kubernetes.io/worker": ""}),
            spec=SimpleNamespace(unschedulable=False, taints=[]),
        )
        assert _node_is_schedulable_worker(node) is True

    def test_rejects_noschedule(self):
        node = SimpleNamespace(
            metadata=SimpleNamespace(labels={"node-role.kubernetes.io/worker": ""}),
            spec=SimpleNamespace(
                unschedulable=False,
                taints=[SimpleNamespace(effect="NoSchedule")],
            ),
        )
        assert _node_is_schedulable_worker(node) is False

    def test_ram_units(self):
        def make(mem):
            return SimpleNamespace(
                status=SimpleNamespace(allocatable={"cpu": "4", "memory": mem})
            )

        assert _node_allocatable_vcpus_ram_mb(make("2048Ki")) == (4, 2)
        assert _node_allocatable_vcpus_ram_mb(make("512Mi")) == (4, 512)
        assert _node_allocatable_vcpus_ram_mb(make("2Gi")) == (4, 2048)

    def test_query_cluster_capacity_fallback(self):
        core_api = MagicMock()
        core_api.list_node.side_effect = RuntimeError("down")
        assert _query_cluster_capacity(core_api) == (256, 1024 * 1024)

    def test_query_cluster_capacity_sums_workers(self):
        worker = SimpleNamespace(
            metadata=SimpleNamespace(labels={"node-role.kubernetes.io/worker": ""}),
            spec=SimpleNamespace(unschedulable=False, taints=[]),
            status=SimpleNamespace(allocatable={"cpu": "8", "memory": "16Gi"}),
        )
        master = SimpleNamespace(
            metadata=SimpleNamespace(labels={"node-role.kubernetes.io/master": ""}),
            spec=SimpleNamespace(unschedulable=False, taints=[]),
            status=SimpleNamespace(allocatable={"cpu": "4", "memory": "8Gi"}),
        )
        core_api = MagicMock()
        core_api.list_node.return_value = SimpleNamespace(items=[worker, master])
        assert _query_cluster_capacity(core_api) == (8, 16384)
