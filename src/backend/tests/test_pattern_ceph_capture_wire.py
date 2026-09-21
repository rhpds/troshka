"""Tests for wiring project Ceph capture into pattern_service's KubeVirt capture.

Heavy-mock style (matches test_pattern_service_coverage.py): db/pattern/project
are MagicMocks, all cluster/S3/operator boundaries are patched, and topology is
the one real input so ``topology_has_ceph`` exercises real logic.

Covers:
  - _prepare_ceph_capture (readiness check, PVC discovery, freeze)
  - _poll_ceph_capture_phase (success / CaptureError / timeout)
  - _finalize_ceph_capture_disks (PatternDisk rows + topology stamp)
  - _capture_kubevirt_native ordering: freeze+ceph capture happens before
    unfreeze, which happens before VM disk export/poll; any Ceph preflight
    or capture failure aborts before VM disk capture ever starts.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.models.pattern import Pattern
from app.models.provider import Provider

TOPOLOGY_WITH_CEPH = {
    "nodes": [
        {"id": "vm-1", "type": "vmNode", "data": {"label": "vm1"}},
        {
            "id": "disk-1",
            "type": "storageNode",
            "data": {"format": "qcow2", "size": 20},
        },
        {"id": "ceph-1", "type": "cephClusterNode", "data": {}},
    ],
    "edges": [{"source": "vm-1", "target": "disk-1"}],
}

TOPOLOGY_NO_CEPH = {
    "nodes": [
        {"id": "vm-1", "type": "vmNode", "data": {"label": "vm1"}},
        {
            "id": "disk-1",
            "type": "storageNode",
            "data": {"format": "qcow2", "size": 20},
        },
    ],
    "edges": [{"source": "vm-1", "target": "disk-1"}],
}


# ═══════════════════════════════════════════════════════════════════════════
# _prepare_ceph_capture
# ═══════════════════════════════════════════════════════════════════════════


class TestPrepareCephCapture:
    def _patch_helpers(self, phase="Ready", devices=None, discover_error=None):
        freeze = MagicMock()
        unfreeze = MagicMock()
        ceph_cluster_phase = MagicMock(return_value=(phase, "fsid-1"))
        discover = MagicMock()
        if discover_error is not None:
            discover.side_effect = discover_error
        else:
            discover.return_value = devices or []

        def is_ready(p):
            return p.lower() in ("ready", "connected")

        return patch(
            "app.services.pattern_service._import_operator_ceph_helpers",
            return_value=(freeze, unfreeze, ceph_cluster_phase, discover, is_ready),
        ), (freeze, unfreeze, ceph_cluster_phase, discover)

    def test_builds_device_manifest_and_freezes(self):
        devices = [
            {
                "name": "rook-ceph-mon-a",
                "kind": "ceph-mon",
                "index": 0,
                "size_bytes": 10 * 1073741824,
            },
            {
                "name": "osd-set-data-0",
                "kind": "ceph-osd",
                "index": 0,
                "size_bytes": 100 * 1073741824,
            },
        ]
        patcher, (freeze, unfreeze, _phase, discover) = self._patch_helpers(
            devices=devices
        )
        with patcher:
            from app.services.pattern_service import _prepare_ceph_capture

            custom_api = MagicMock()
            core_api = MagicMock()
            ceph_devices = _prepare_ceph_capture(custom_api, core_api, "ns-1", "pat-1")

        assert len(ceph_devices) == 2
        mon = next(d for d in ceph_devices if d["kind"] == "ceph-mon")
        osd = next(d for d in ceph_devices if d["kind"] == "ceph-osd")
        assert mon["pvcName"] == "rook-ceph-mon-a"
        assert mon["s3Key"] == "patterns/pat-1/ceph-ceph-mon-0.tar.gz"
        assert mon["sizeGb"] == 10
        assert osd["s3Key"] == "patterns/pat-1/ceph-ceph-osd-0.qcow2"
        assert osd["sizeGb"] == 100
        # patternDiskId pre-assigned and unique per device
        assert mon["patternDiskId"] != osd["patternDiskId"]
        assert mon["patternDiskId"]

        freeze.assert_called_once()
        assert freeze.call_args.args[0] == "ns-1"
        assert freeze.call_args.kwargs["core_api"] is core_api
        unfreeze.assert_not_called()
        discover.assert_called_once_with(core_api, "ns-1", custom_api=custom_api)

    def test_not_ready_raises_without_freezing(self):
        patcher, (freeze, _unfreeze, _phase, _discover) = self._patch_helpers(
            phase="Degraded"
        )
        with patcher:
            from app.services.pattern_service import _prepare_ceph_capture

            with pytest.raises(RuntimeError, match="not Ready"):
                _prepare_ceph_capture(MagicMock(), MagicMock(), "ns-1", "pat-1")
        freeze.assert_not_called()

    def test_pvc_mismatch_raises_without_freezing(self):
        patcher, (freeze, _unfreeze, _phase, _discover) = self._patch_helpers(
            discover_error=ValueError("expected 3 ceph-osd PVC(s), found 1")
        )
        with patcher:
            from app.services.pattern_service import _prepare_ceph_capture

            with pytest.raises(ValueError, match="expected 3 ceph-osd"):
                _prepare_ceph_capture(MagicMock(), MagicMock(), "ns-1", "pat-1")
        freeze.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _poll_ceph_capture_phase
# ═══════════════════════════════════════════════════════════════════════════


class TestPollCephCapturePhase:
    def test_returns_captured_disks_on_success(self):
        from app.services.pattern_service import _poll_ceph_capture_phase

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "Capturing",
                "capturedCephDisks": [{"kind": "ceph-mon"}],
            }
        }
        with patch("time.sleep"):
            result = _poll_ceph_capture_phase(
                custom_api,
                "ns-1",
                "cr-1",
                "pat-1",
                "troshka.redhat.com",
                "v1alpha1",
                30,
            )
        assert result == [{"kind": "ceph-mon"}]

    def test_returns_none_on_capture_error(self):
        from app.services.pattern_service import _poll_ceph_capture_phase

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "CaptureError", "captureError": "device boom"}
        }
        with patch("time.sleep"):
            result = _poll_ceph_capture_phase(
                custom_api,
                "ns-1",
                "cr-1",
                "pat-1",
                "troshka.redhat.com",
                "v1alpha1",
                30,
            )
        assert result is None

    def test_returns_none_on_timeout(self):
        from app.services.pattern_service import _poll_ceph_capture_phase

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"phase": "Capturing"}
        }
        with patch("time.sleep"):
            result = _poll_ceph_capture_phase(
                custom_api, "ns-1", "cr-1", "pat-1", "troshka.redhat.com", "v1alpha1", 5
            )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# _finalize_ceph_capture_disks
# ═══════════════════════════════════════════════════════════════════════════


class TestFinalizeCephCaptureDisks:
    def test_creates_disk_rows_and_stamps_topology(self):
        from app.services.pattern_service import _finalize_ceph_capture_disks

        ceph_devices = [
            {
                "pvcName": "rook-ceph-mon-a",
                "kind": "ceph-mon",
                "index": 0,
                "patternDiskId": "pd-mon",
            },
            {
                "pvcName": "osd-set-data-0",
                "kind": "ceph-osd",
                "index": 0,
                "patternDiskId": "pd-osd0",
            },
            {
                "pvcName": "osd-set-data-1",
                "kind": "ceph-osd",
                "index": 1,
                "patternDiskId": "pd-osd1",
            },
        ]
        captured_ceph_disks = [
            {
                "patternDiskId": "pd-mon",
                "kind": "ceph-mon",
                "index": 0,
                "s3Key": "patterns/pat-1/ceph-ceph-mon-0.tar.gz",
                "format": "tar.gz",
                "sizeBytes": 1_000_000,
                "virtualSizeBytes": 2_000_000,
            },
            {
                "patternDiskId": "pd-osd0",
                "kind": "ceph-osd",
                "index": 0,
                "s3Key": "patterns/pat-1/ceph-ceph-osd-0.qcow2",
                "format": "qcow2",
                "sizeBytes": 5_000_000,
                "virtualSizeBytes": 9_000_000,
            },
            {
                "patternDiskId": "pd-osd1",
                "kind": "ceph-osd",
                "index": 1,
                "s3Key": "patterns/pat-1/ceph-ceph-osd-1.qcow2",
                "format": "qcow2",
                "sizeBytes": 6_000_000,
                "virtualSizeBytes": 9_000_000,
            },
        ]

        db = MagicMock()
        pattern = MagicMock()
        pattern.id = "pat-1"
        pattern.source_provider_id = "prov-1"
        pattern.topology = {"projectCeph": {"phase": "Ready"}, "nodes": []}

        total_size = _finalize_ceph_capture_disks(
            db, pattern, ceph_devices, captured_ceph_disks
        )

        assert total_size == 1_000_000 + 5_000_000 + 6_000_000

        added_disks = [
            c.args[0]
            for c in db.add.call_args_list
            if type(c.args[0]).__name__ == "PatternDisk"
        ]
        assert len(added_disks) == 3
        mon_disk = next(d for d in added_disks if d.source_kind == "ceph-mon")
        assert mon_disk.id == "pd-mon"
        assert mon_disk.source_disk_id == "ceph-ceph-mon-0"
        assert mon_disk.source_vm_id is None
        assert mon_disk.source_index == 0
        assert mon_disk.source_pvc_name == "rook-ceph-mon-a"
        assert mon_disk.state == "available"

        osd_disks = [d for d in added_disks if d.source_kind == "ceph-osd"]
        assert {d.source_index for d in osd_disks} == {0, 1}

        added_locations = [
            c.args[0]
            for c in db.add.call_args_list
            if type(c.args[0]).__name__ == "PatternLocation"
        ]
        assert len(added_locations) == 3
        assert all(loc.provider_id == "prov-1" for loc in added_locations)

        # Runtime projectCeph stripped, capture stamp added, in index order.
        assert "projectCeph" not in pattern.topology
        capture_stamp = pattern.topology["projectCephCapture"]
        assert capture_stamp["monDiskId"] == "pd-mon"
        assert capture_stamp["osdDiskIds"] == ["pd-osd0", "pd-osd1"]

        db.execute.assert_called_once()
        db.commit.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# _capture_kubevirt_native — full wiring / ordering
# ═══════════════════════════════════════════════════════════════════════════


class TestCaptureKubevirtNativeCephWiring:
    def _make_provider(self):
        provider = MagicMock()
        provider.id = "prov-1"
        provider.name = "prov-1"
        return provider

    def _make_pattern(self, topology):
        pattern = MagicMock()
        pattern.id = "pat-1"
        pattern.topology = dict(topology)
        pattern.source_provider_id = None
        return pattern

    def _make_project(self, topology):
        project = MagicMock()
        project.id = "proj-1"
        project.deployed_topology = topology
        project.topology = topology
        return project

    def _make_host(self):
        host = MagicMock()
        host.id = "host-1"
        host.provider_id = "prov-1"
        host.storage_pool_id = None
        return host

    def _make_db(self, provider, pattern):
        db = MagicMock()

        def _query(model):
            m = MagicMock()
            if model is Provider:
                m.filter_by.return_value.first.return_value = provider
            elif model is Pattern:
                m.filter_by.return_value.first.return_value = pattern
            else:
                m.filter_by.return_value.first.return_value = None
                m.filter_by.return_value.all.return_value = []
            return m

        db.query.side_effect = _query
        return db

    def _common_patches(self):
        return (
            patch(
                "app.services.providers.kubevirt._get_k8s_clients",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch(
                "app.services.providers.kubevirt._project_ns",
                return_value="troshka-proj1",
            ),
            patch("app.services.providers.kubevirt._ensure_s3_secret"),
            patch(
                "app.services.s3_storage.get_cluster_s3_config",
                return_value={
                    "access_key_id": "ak",
                    "secret_access_key": "sk",
                    "region": "us-east-1",
                    "endpoint": "http://s3.local",
                    "bucket": "bucket1",
                },
            ),
            patch("app.services.ws_pubsub.notify_pattern"),
            patch("app.services.pattern_service._save_pattern_metadata_to_s3"),
            patch("app.services.pattern_service._enqueue_pattern_sync"),
        )

    def test_happy_path_freezes_captures_ceph_then_unfreezes_before_vm_disks(self):
        from app.services.pattern_service import _capture_kubevirt_native

        provider = self._make_provider()
        pattern = self._make_pattern(TOPOLOGY_WITH_CEPH)
        project = self._make_project(TOPOLOGY_WITH_CEPH)
        host = self._make_host()
        db = self._make_db(provider, pattern)

        order = []
        ceph_devices = [
            {
                "pvcName": "rook-ceph-mon-a",
                "kind": "ceph-mon",
                "index": 0,
                "patternDiskId": "pd-mon",
                "sizeGb": 10,
            },
        ]
        vm_captured = [
            {
                "diskId": "disk-1",
                "vmId": "vm-1",
                "s3Key": "patterns/pat-1/disk-1.qcow2",
                "format": "qcow2",
                "sizeBytes": 1000,
                "virtualSizeBytes": 2000,
            }
        ]

        def _prepare(*_a, **_k):
            order.append("prepare_ceph")
            return ceph_devices

        def _poll_ceph(*_a, **_k):
            order.append("poll_ceph")
            return [
                {
                    "patternDiskId": "pd-mon",
                    "kind": "ceph-mon",
                    "index": 0,
                    "sizeBytes": 999,
                }
            ]

        def _finalize(*_a, **_k):
            order.append("finalize_ceph")
            return 999

        def _unfreeze(*_a, **_k):
            order.append("unfreeze")

        def _poll_vm(*_a, **_k):
            order.append("poll_vm")
            return vm_captured

        patches = self._common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch(
                "app.services.pattern_service._prepare_ceph_capture",
                side_effect=_prepare,
            ),
            patch(
                "app.services.pattern_service._poll_ceph_capture_phase",
                side_effect=_poll_ceph,
            ),
            patch(
                "app.services.pattern_service._finalize_ceph_capture_disks",
                side_effect=_finalize,
            ),
            patch(
                "app.services.pattern_service._unfreeze_ceph_best_effort",
                side_effect=_unfreeze,
            ),
            patch(
                "app.services.pattern_service._poll_capture_completion",
                side_effect=_poll_vm,
            ),
        ):
            _capture_kubevirt_native(db, pattern, project, host, restart_after=False)

        assert order == [
            "prepare_ceph",
            "poll_ceph",
            "finalize_ceph",
            "unfreeze",
            "poll_vm",
        ]
        assert pattern.state == "available"
        assert pattern.total_size_bytes == 1000 + 999

    def test_ceph_preflight_failure_fails_capture_before_annotation(self):
        from app.services.pattern_service import _capture_kubevirt_native

        provider = self._make_provider()
        pattern = self._make_pattern(TOPOLOGY_WITH_CEPH)
        project = self._make_project(TOPOLOGY_WITH_CEPH)
        host = self._make_host()
        db = self._make_db(provider, pattern)

        mock_custom_api = MagicMock()
        patches = self._common_patches()
        with (
            patch(
                "app.services.providers.kubevirt._get_k8s_clients",
                return_value=(mock_custom_api, MagicMock(), MagicMock()),
            ),
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch(
                "app.services.pattern_service._prepare_ceph_capture",
                side_effect=RuntimeError("project Ceph not Ready (phase=Degraded)"),
            ) as mock_prepare,
            patch(
                "app.services.pattern_service._unfreeze_ceph_best_effort"
            ) as mock_unfreeze,
            patch(
                "app.services.pattern_service._poll_ceph_capture_phase"
            ) as mock_poll_ceph,
            patch(
                "app.services.pattern_service._poll_capture_completion"
            ) as mock_poll_vm,
        ):
            _capture_kubevirt_native(db, pattern, project, host, restart_after=False)

        mock_prepare.assert_called_once()
        mock_custom_api.patch_namespaced_custom_object.assert_not_called()
        mock_poll_ceph.assert_not_called()
        mock_poll_vm.assert_not_called()
        mock_unfreeze.assert_called_once()
        assert pattern.state == "error"

    def test_ceph_capture_phase_failure_aborts_before_vm_disk_capture(self):
        from app.services.pattern_service import _capture_kubevirt_native

        provider = self._make_provider()
        pattern = self._make_pattern(TOPOLOGY_WITH_CEPH)
        project = self._make_project(TOPOLOGY_WITH_CEPH)
        host = self._make_host()
        db = self._make_db(provider, pattern)

        ceph_devices = [
            {
                "pvcName": "rook-ceph-mon-a",
                "kind": "ceph-mon",
                "index": 0,
                "patternDiskId": "pd-mon",
                "sizeGb": 10,
            },
        ]

        patches = self._common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch(
                "app.services.pattern_service._prepare_ceph_capture",
                return_value=ceph_devices,
            ),
            patch(
                "app.services.pattern_service._poll_ceph_capture_phase",
                return_value=None,
            ) as mock_poll_ceph,
            patch(
                "app.services.pattern_service._finalize_ceph_capture_disks"
            ) as mock_finalize,
            patch(
                "app.services.pattern_service._unfreeze_ceph_best_effort"
            ) as mock_unfreeze,
            patch(
                "app.services.pattern_service._poll_capture_completion"
            ) as mock_poll_vm,
        ):
            _capture_kubevirt_native(db, pattern, project, host, restart_after=False)

        mock_poll_ceph.assert_called_once()
        mock_finalize.assert_not_called()
        mock_poll_vm.assert_not_called()
        mock_unfreeze.assert_called_once()
        assert pattern.state == "error"

    def test_no_ceph_in_topology_skips_ceph_orchestration(self):
        from app.services.pattern_service import _capture_kubevirt_native

        provider = self._make_provider()
        pattern = self._make_pattern(TOPOLOGY_NO_CEPH)
        project = self._make_project(TOPOLOGY_NO_CEPH)
        host = self._make_host()
        db = self._make_db(provider, pattern)

        vm_captured = [
            {
                "diskId": "disk-1",
                "vmId": "vm-1",
                "s3Key": "patterns/pat-1/disk-1.qcow2",
                "format": "qcow2",
                "sizeBytes": 1000,
                "virtualSizeBytes": 2000,
            }
        ]

        patches = self._common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch("app.services.pattern_service._prepare_ceph_capture") as mock_prepare,
            patch(
                "app.services.pattern_service._unfreeze_ceph_best_effort"
            ) as mock_unfreeze,
            patch(
                "app.services.pattern_service._poll_capture_completion",
                return_value=vm_captured,
            ),
        ):
            _capture_kubevirt_native(db, pattern, project, host, restart_after=False)

        mock_prepare.assert_not_called()
        mock_unfreeze.assert_not_called()
        assert pattern.state == "available"
        assert pattern.total_size_bytes == 1000
