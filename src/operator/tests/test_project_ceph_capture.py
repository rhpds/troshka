"""Tests for parallel Ceph mon/OSD device snapshot + export capture.

Covers the per-device pipeline (snapshot -> temp PVC -> qemu-img/tar convert
-> rclone) and the bounded-concurrency orchestrator in handlers/project.py,
plus the Ceph-specific job/PVC builders in helpers/patterns.py.
"""

import asyncio

import pytest
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException


# ---------------------------------------------------------------------------
# helpers/patterns.py — Ceph device builders
# ---------------------------------------------------------------------------


class TestBuildTempPvcFromSnapshotVolumeMode:
    def test_default_omits_volume_mode(self):
        from helpers.patterns import build_temp_pvc_from_snapshot

        pvc = build_temp_pvc_from_snapshot("p", "ns", "snap", 10)
        assert "volumeMode" not in pvc["spec"]

    def test_block_mode_set_for_osd_restore(self):
        from helpers.patterns import build_temp_pvc_from_snapshot

        pvc = build_temp_pvc_from_snapshot("p", "ns", "snap", 10, volume_mode="Block")
        assert pvc["spec"]["volumeMode"] == "Block"


class TestBuildCephDeviceExportJob:
    def test_osd_uses_block_device_and_qemu_img(self):
        from helpers.patterns import build_ceph_device_export_job

        job = build_ceph_device_export_job(
            "ceph-ceph-osd-0", "ns1", "temp-pvc", "patterns/p/osd0.qcow2",
            {"bucket": "b"}, 20, block_device=True,
        )
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert container["volumeDevices"] == [
            {"name": "disk", "devicePath": "/dev/cephdisk"}
        ]
        assert container["volumeMounts"] == [
            {"name": "scratch", "mountPath": "/scratch"}
        ]
        assert "qemu-img convert" in container["command"][2]
        assert "-S 4k" in container["command"][2]
        assert job["metadata"]["name"] == "export-ceph-ceph-osd-0"
        assert job["metadata"]["labels"] == {"troshka-role": "pattern-export"}

    def test_mon_uses_filesystem_mount_and_tar(self):
        from helpers.patterns import build_ceph_device_export_job

        job = build_ceph_device_export_job(
            "ceph-ceph-mon-0", "ns1", "temp-pvc", "patterns/p/mon.tar.gz",
            {"bucket": "b"}, 10, block_device=False,
        )
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert container["volumeMounts"] == [
            {"name": "disk", "mountPath": "/disk"},
            {"name": "scratch", "mountPath": "/scratch"},
        ]
        assert "volumeDevices" not in container
        assert "tar -C /disk -czf" in container["command"][2]

    def test_deadline_and_scratch_pvc_name_present(self):
        from helpers.patterns import build_ceph_device_export_job

        job = build_ceph_device_export_job(
            "ceph-ceph-osd-1", "ns1", "temp-pvc", "k", {"bucket": "b"}, 50,
            block_device=True,
        )
        assert job["_deadline"] >= 3600
        assert job["_scratchPvcName"] == "scratch-ceph-ceph-osd-1"


# ---------------------------------------------------------------------------
# handlers/project.py — per-device pipeline
# ---------------------------------------------------------------------------


def _osd_device(index=0, disk_id="disk-0"):
    return {
        "pvcName": f"troshka-ceph-osd-{index}",
        "kind": "ceph-osd",
        "index": index,
        "patternDiskId": disk_id,
        "s3Key": f"patterns/p/osd-{index}.qcow2",
    }


def _mon_device():
    return {
        "pvcName": "troshka-ceph-mon",
        "kind": "ceph-mon",
        "index": 0,
        "patternDiskId": "disk-mon",
        "s3Key": "patterns/p/mon.tar.gz",
    }


class TestSnapshotAndExportCephDevice:
    @patch(
        "helpers.patterns.build_ceph_device_export_job",
        return_value={"metadata": {"name": "export-ceph-ceph-osd-0"}},
    )
    @patch(
        "helpers.patterns.build_temp_pvc_from_snapshot",
        return_value={"metadata": {"name": "ceph-export-ceph-osd-0"}},
    )
    @patch(
        "helpers.patterns.build_volume_snapshot",
        return_value={"metadata": {"name": "ceph-snap-ceph-osd-0"}},
    )
    def test_creates_snapshot_pvc_and_job_for_osd(self, mock_snap, mock_pvc, mock_job):
        from handlers.project import _snapshot_and_export_ceph_device

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"readyToUse": True}
        }
        core_api = MagicMock()
        batch_api = MagicMock()

        result = asyncio.run(
            _snapshot_and_export_ceph_device(
                _osd_device(), {"bucket": "b"}, custom_api, core_api, batch_api,
                "ns1", "proj1",
            )
        )

        custom_api.create_namespaced_custom_object.assert_called_once()
        assert core_api.create_namespaced_persistent_volume_claim.call_count == 2
        batch_api.create_namespaced_job.assert_called_once()
        assert result["diskId"] == "disk-0"
        assert result["cephKind"] == "ceph-osd"
        assert result["cephIndex"] == 0
        assert result["format"] == "qcow2"
        assert result["vmId"] == ""

    def test_osd_restores_into_block_mode_temp_pvc(self):
        from handlers.project import _snapshot_and_export_ceph_device

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"readyToUse": True}
        }
        core_api = MagicMock()
        batch_api = MagicMock()

        asyncio.run(
            _snapshot_and_export_ceph_device(
                _osd_device(), {"bucket": "b"}, custom_api, core_api, batch_api,
                "ns1", "proj1",
            )
        )

        created_pvcs = [
            call.kwargs["body"]
            for call in core_api.create_namespaced_persistent_volume_claim.call_args_list
        ]
        temp_pvc = next(p for p in created_pvcs if p["metadata"]["name"].startswith("ceph-export-"))
        assert temp_pvc["spec"]["volumeMode"] == "Block"

    def test_mon_restores_into_filesystem_temp_pvc(self):
        from handlers.project import _snapshot_and_export_ceph_device

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"readyToUse": True}
        }
        core_api = MagicMock()
        batch_api = MagicMock()

        result = asyncio.run(
            _snapshot_and_export_ceph_device(
                _mon_device(), {"bucket": "b"}, custom_api, core_api, batch_api,
                "ns1", "proj1",
            )
        )

        created_pvcs = [
            call.kwargs["body"]
            for call in core_api.create_namespaced_persistent_volume_claim.call_args_list
        ]
        temp_pvc = next(p for p in created_pvcs if p["metadata"]["name"].startswith("ceph-export-"))
        assert "volumeMode" not in temp_pvc["spec"]
        assert result["format"] == "tar.gz"

    def test_handles_409_conflict_on_snapshot(self):
        from handlers.project import _snapshot_and_export_ceph_device

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=409, reason="Conflict"
        )
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {"readyToUse": True}
        }
        core_api = MagicMock()
        batch_api = MagicMock()

        result = asyncio.run(
            _snapshot_and_export_ceph_device(
                _osd_device(), {"bucket": "b"}, custom_api, core_api, batch_api,
                "ns1", "proj1",
            )
        )
        assert "jobName" in result

    def test_raises_on_non_conflict_error(self):
        from handlers.project import _snapshot_and_export_ceph_device

        custom_api = MagicMock()
        custom_api.create_namespaced_custom_object.side_effect = ApiException(
            status=500, reason="boom"
        )
        core_api = MagicMock()
        batch_api = MagicMock()

        with pytest.raises(ApiException):
            asyncio.run(
                _snapshot_and_export_ceph_device(
                    _osd_device(), {"bucket": "b"}, custom_api, core_api, batch_api,
                    "ns1", "proj1",
                )
            )


# ---------------------------------------------------------------------------
# handlers/project.py — bounded-concurrency orchestrator
# ---------------------------------------------------------------------------


def _fake_export_job(device_info):
    idx = device_info["index"]
    kind = device_info["kind"]
    return {
        "jobName": f"export-ceph-{kind}-{idx}",
        "snapName": f"snap-{idx}",
        "tempPvcName": f"tmp-{idx}",
        "scratchPvcName": f"scratch-{idx}",
        "diskId": device_info["patternDiskId"],
        "vmId": "",
        "s3Key": device_info["s3Key"],
        "format": "qcow2",
        "virtualSizeBytes": 1073741824,
        "deadline": 3600,
        "displayName": f"{kind}-{idx}",
        "cephKind": kind,
        "cephIndex": idx,
    }


class TestCephCaptureDiskRows:
    def test_pending_snapshotting_and_job_progress(self):
        from handlers.project import _ceph_capture_disk_rows

        devices = [_mon_device(), _osd_device(0), _osd_device(1)]
        created = [
            {
                "jobName": "export-ceph-ceph-osd-0",
                "cephKind": "ceph-osd",
                "cephIndex": 0,
                "displayName": "ceph-osd-0",
            }
        ]
        disk_statuses = {"export-ceph-ceph-osd-0": "converting 40%"}
        device_phase = {
            ("ceph-mon", 0): "snapshotting",
            ("ceph-osd", 1): "pending",
        }
        rows = _ceph_capture_disk_rows(
            devices, created, disk_statuses, device_phase
        )
        assert rows == [
            {"name": "ceph-mon-0", "status": "snapshotting"},
            {"name": "ceph-osd-0", "status": "converting 40%"},
            {"name": "ceph-osd-1", "status": "pending"},
        ]

    def test_patch_publishes_capture_disks(self):
        from handlers.project import _patch_ceph_capture_progress

        custom_api = MagicMock()
        devices = [_osd_device(0), _osd_device(1)]
        with patch("handlers.project._patch_cr_status") as mock_patch:
            _patch_ceph_capture_progress(
                custom_api, "ns1", "proj1", devices, [], {},
                {("ceph-osd", 0): "pending", ("ceph-osd", 1): "pending"},
            )
        body = mock_patch.call_args[0][3]
        assert body["captureProgress"] == "Capturing project Ceph (0/2)"
        assert body["captureDisks"] == [
            {"name": "ceph-osd-0", "status": "pending"},
            {"name": "ceph-osd-1", "status": "pending"},
        ]


class TestCaptureCephDevicesConcurrency:
    def test_snapshots_issued_without_waiting_for_export_completion(self):
        """3 devices, concurrency cap min(3,4)=3: all 3 snapshot creates must be
        issued before any device's export job is allowed to finish — proving
        device 1/2 aren't serialized behind device 0's full export."""
        from handlers.project import _capture_ceph_devices

        devices = [_osd_device(index=i, disk_id=f"disk-{i}") for i in range(3)]
        snapshot_calls = []
        release_event = asyncio.Event()

        async def fake_snapshot_and_export(device_info, *_args, **_kwargs):
            snapshot_calls.append(device_info["index"])
            return _fake_export_job(device_info)

        async def fake_await_export(*_args, **_kwargs):
            await release_event.wait()
            return None

        with (
            patch(
                "handlers.project._snapshot_and_export_ceph_device",
                side_effect=fake_snapshot_and_export,
            ),
            patch(
                "handlers.project._await_ceph_export_job",
                side_effect=fake_await_export,
            ),
            patch("handlers.project._patch_cr_status"),
            patch("handlers.project._cleanup_capture_resources"),
            patch("handlers.project._read_export_sizes"),
        ):

            async def run():
                task = asyncio.create_task(
                    _capture_ceph_devices(
                        devices, {}, MagicMock(), MagicMock(), MagicMock(),
                        "ns1", "proj1",
                    )
                )
                for _ in range(10):
                    await asyncio.sleep(0)
                assert sorted(snapshot_calls) == [0, 1, 2]
                release_event.set()
                return await task

            result = asyncio.run(run())

        assert len(result) == 3
        assert {d["index"] for d in result} == {0, 1, 2}

    def test_concurrency_capped_at_ceph_capture_concurrency(self):
        """6 devices > CEPH_CAPTURE_CONCURRENCY (4): no more than 4 device
        pipelines should be in-flight (snapshot done, export not yet done) at
        once."""
        from handlers.project import _capture_ceph_devices

        devices = [_osd_device(index=i, disk_id=f"disk-{i}") for i in range(6)]
        in_flight = 0
        max_in_flight = 0

        async def fake_snapshot_and_export(device_info, *_args, **_kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            return _fake_export_job(device_info)

        async def fake_await_export(*_args, **_kwargs):
            nonlocal in_flight
            await asyncio.sleep(0.05)
            in_flight -= 1
            return None

        with (
            patch(
                "handlers.project._snapshot_and_export_ceph_device",
                side_effect=fake_snapshot_and_export,
            ),
            patch(
                "handlers.project._await_ceph_export_job",
                side_effect=fake_await_export,
            ),
            patch("handlers.project._patch_cr_status"),
            patch("handlers.project._cleanup_capture_resources"),
            patch("handlers.project._read_export_sizes"),
        ):
            result = asyncio.run(
                _capture_ceph_devices(
                    devices, {}, MagicMock(), MagicMock(), MagicMock(), "ns1", "proj1"
                )
            )

        assert max_in_flight == 4
        assert len(result) == 6

    def test_empty_device_list_returns_empty(self):
        from handlers.project import _capture_ceph_devices

        result = asyncio.run(
            _capture_ceph_devices(
                [], {}, MagicMock(), MagicMock(), MagicMock(), "ns1", "proj1"
            )
        )
        assert result == []

    def test_any_device_failure_fails_whole_batch_no_partial_commit(self):
        """One failing device must fail the entire Ceph capture — even though
        the other two devices' exports succeeded, no captured disks are
        returned and all created resources are cleaned up."""
        from handlers.project import _capture_ceph_devices

        devices = [_osd_device(index=i, disk_id=f"disk-{i}") for i in range(3)]

        async def fake_snapshot_and_export(device_info, *_args, **_kwargs):
            return _fake_export_job(device_info)

        async def fake_await_export(_batch_api, ej, *_args, **_kwargs):
            if ej["cephIndex"] == 1:
                return "export job export-ceph-ceph-osd-1 failed"
            return None

        cleanup_mock = MagicMock()
        with (
            patch(
                "handlers.project._snapshot_and_export_ceph_device",
                side_effect=fake_snapshot_and_export,
            ),
            patch(
                "handlers.project._await_ceph_export_job",
                side_effect=fake_await_export,
            ),
            patch("handlers.project._patch_cr_status"),
            patch("handlers.project._cleanup_capture_resources", cleanup_mock),
            patch("handlers.project._read_export_sizes"),
        ):
            with pytest.raises(RuntimeError):
                asyncio.run(
                    _capture_ceph_devices(
                        devices, {}, MagicMock(), MagicMock(), MagicMock(),
                        "ns1", "proj1",
                    )
                )

        # Cleanup must have run over all 3 created job descriptors (the two
        # that "succeeded" and the one that failed), not just the failure.
        cleanup_args = cleanup_mock.call_args[0]
        created_jobs = cleanup_args[3]
        assert len(created_jobs) == 3


class TestRunCephCapturePhase:
    def test_no_devices_continues(self):
        from handlers.project import _run_ceph_capture_phase

        patch_obj = MagicMock()
        patch_obj.status = {}
        result = asyncio.run(
            _run_ceph_capture_phase(
                [], {}, MagicMock(), MagicMock(), MagicMock(), "ns1", "proj1",
                patch_obj,
            )
        )
        assert result is True

    @patch("handlers.project._clear_capture_annotation")
    @patch("handlers.project._capture_ceph_devices")
    def test_failure_clears_annotation_and_returns_false(
        self, mock_capture, mock_clear
    ):
        from handlers.project import _run_ceph_capture_phase

        mock_capture.side_effect = RuntimeError("device failed")
        patch_obj = MagicMock()
        patch_obj.status = {}
        custom_api = MagicMock()

        result = asyncio.run(
            _run_ceph_capture_phase(
                [_osd_device()], {}, custom_api, MagicMock(), MagicMock(),
                "ns1", "proj1", patch_obj,
            )
        )

        assert result is False
        mock_clear.assert_called_once_with(custom_api, "ns1", "proj1")
        assert "capturedCephDisks" not in patch_obj.status

    @patch("handlers.project._capture_ceph_devices")
    def test_success_stores_captured_ceph_disks_on_patch(self, mock_capture):
        from handlers.project import _run_ceph_capture_phase

        mock_capture.return_value = [
            {"patternDiskId": "disk-0", "kind": "ceph-osd", "index": 0}
        ]
        patch_obj = MagicMock()
        patch_obj.status = {}

        with patch("handlers.project._patch_cr_status"):
            result = asyncio.run(
                _run_ceph_capture_phase(
                    [_osd_device()], {}, MagicMock(), MagicMock(), MagicMock(),
                    "ns1", "proj1", patch_obj,
                )
            )

        assert result is True
        assert patch_obj.status["capturedCephDisks"] == mock_capture.return_value


class TestHandleCaptureCephWiring:
    @patch("handlers.project.client")
    @patch("handlers.project._run_ceph_capture_phase")
    def test_ceph_failure_aborts_before_vm_disk_export(
        self, mock_ceph_phase, _mock_client
    ):
        """When the Ceph capture phase fails, _handle_capture must return
        immediately and never call the VM disk snapshot/export pipeline."""
        from handlers.project import _handle_capture

        mock_ceph_phase.return_value = False

        with patch("handlers.project._snapshot_and_export_disk") as mock_vm_disk:
            asyncio.run(
                _handle_capture(
                    {
                        "s3Config": {"bucket": "b"},
                        "disks": [
                            {
                                "pvcName": "p",
                                "diskId": "d1",
                                "vmName": "vm1",
                                "s3Key": "k",
                            }
                        ],
                        "cephDevices": [_osd_device()],
                    },
                    "ns1",
                    "proj1",
                    MagicMock(status={}),
                )
            )
            mock_vm_disk.assert_not_called()

    @patch("handlers.project.client")
    @patch("handlers.project._poll_export_jobs", return_value=None)
    @patch("handlers.project._read_export_sizes")
    @patch("handlers.project._cleanup_capture_resources")
    @patch("handlers.project._clear_capture_annotation")
    @patch("handlers.project._run_ceph_capture_phase", return_value=True)
    def test_ceph_success_proceeds_to_vm_disk_export(
        self, mock_ceph_phase, _mock_clear, _mock_cleanup, _mock_sizes,
        _mock_poll, _mock_client,
    ):
        from handlers.project import _handle_capture

        async def fake_vm_export(disk_info, *_args, **_kwargs):
            return {
                "jobName": "export-vm1-d1",
                "snapName": "s",
                "tempPvcName": "t",
                "scratchPvcName": "sc",
                "diskId": disk_info["diskId"],
                "vmId": "vm-1",
                "s3Key": disk_info["s3Key"],
                "format": "qcow2",
                "virtualSizeBytes": 1024,
                "deadline": 3600,
                "displayName": "vm1/d1",
            }

        patch_obj = MagicMock()
        patch_obj.status = {}
        with patch(
            "handlers.project._snapshot_and_export_disk", side_effect=fake_vm_export
        ) as mock_vm_disk:
            asyncio.run(
                _handle_capture(
                    {
                        "s3Config": {"bucket": "b"},
                        "disks": [
                            {
                                "pvcName": "p",
                                "diskId": "d1",
                                "vmName": "vm1",
                                "s3Key": "k",
                            }
                        ],
                        "cephDevices": [_osd_device()],
                    },
                    "ns1",
                    "proj1",
                    patch_obj,
                )
            )
            mock_vm_disk.assert_called_once()

        assert patch_obj.status["phase"] == "CaptureComplete"
