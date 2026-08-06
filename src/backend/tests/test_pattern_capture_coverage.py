"""Tests for uncovered paths in pattern_service.py — capture helpers.

Covers:
  - _build_capture_disk_manifest
  - cancel_capture job cancellation path
  - _poll_capture_completion
"""

from unittest.mock import MagicMock, patch

from app.services.pattern_service import _build_capture_disk_manifest

# ═══════════════════════════════════════════════════════════════════════════
# _build_capture_disk_manifest
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildCaptureDiskManifest:
    def test_builds_manifest_for_qcow2(self):
        disk_nodes = [
            {"id": "disk-1111", "data": {"format": "qcow2", "size": 100}},
            {"id": "disk-2222", "data": {"format": "qcow2", "size": 50}},
        ]
        disk_to_vm = {"disk-1111": "vm-aaaa", "disk-2222": "vm-bbbb"}
        result = _build_capture_disk_manifest(disk_nodes, disk_to_vm, "pattern-123")
        assert len(result) == 2
        assert result[0]["vmName"] == "vm-vm-aaaa"
        assert result[0]["diskId"] == "disk-1111"
        assert result[0]["s3Key"] == "patterns/pattern-123/disk-1111.qcow2"
        assert result[0]["sizeGb"] == 100

    def test_skips_iso_disks(self):
        disk_nodes = [
            {"id": "disk-iso", "data": {"format": "iso", "size": 4}},
            {"id": "disk-qcow", "data": {"format": "qcow2", "size": 50}},
        ]
        disk_to_vm = {"disk-iso": "vm-1", "disk-qcow": "vm-2"}
        result = _build_capture_disk_manifest(disk_nodes, disk_to_vm, "pat-1")
        assert len(result) == 1
        assert result[0]["diskId"] == "disk-qcow"

    def test_skips_unmapped_disks(self):
        disk_nodes = [
            {"id": "disk-orphan", "data": {"format": "qcow2", "size": 50}},
        ]
        disk_to_vm = {}  # no mapping
        result = _build_capture_disk_manifest(disk_nodes, disk_to_vm, "pat-2")
        assert len(result) == 0

    def test_empty_input(self):
        result = _build_capture_disk_manifest([], {}, "pat-3")
        assert result == []

    def test_default_format_and_size(self):
        disk_nodes = [{"id": "disk-def", "data": {}}]
        disk_to_vm = {"disk-def": "vm-1"}
        result = _build_capture_disk_manifest(disk_nodes, disk_to_vm, "pat-4")
        assert len(result) == 1
        assert result[0]["format"] == "qcow2"
        assert result[0]["sizeGb"] == 50  # default


# ═══════════════════════════════════════════════════════════════════════════
# _poll_capture_completion
# ═══════════════════════════════════════════════════════════════════════════


class TestPollCaptureCompletion:
    @patch("app.services.ws_pubsub.notify_pattern")
    def test_success_returns_disks(self, mock_notify):
        from app.services.pattern_service import _poll_capture_completion

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "CaptureComplete",
                "capturedDisks": [{"diskId": "d1", "s3Key": "patterns/p1/d1.qcow2"}],
            }
        }
        with patch("time.sleep"):
            result = _poll_capture_completion(
                custom_api, "ns-1", "cr-1", "pattern-1", "troshka.dev", "v1"
            )
        assert result is not None
        assert len(result) == 1
        assert result[0]["diskId"] == "d1"

    @patch("app.services.ws_pubsub.notify_pattern")
    def test_error_returns_none(self, mock_notify):
        from app.services.pattern_service import _poll_capture_completion

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {
            "status": {
                "phase": "CaptureError",
                "captureError": "disk full",
            }
        }
        with patch("time.sleep"):
            result = _poll_capture_completion(
                custom_api, "ns-1", "cr-1", "pattern-2", "troshka.dev", "v1"
            )
        assert result is None

    @patch("app.services.ws_pubsub.notify_pattern")
    def test_progress_update(self, mock_notify):
        from app.services.pattern_service import _poll_capture_completion

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                return {
                    "status": {
                        "phase": "CaptureComplete",
                        "capturedDisks": [],
                        "captureProgress": "50%",
                    }
                }
            return {
                "status": {
                    "phase": "Capturing",
                    "captureProgress": "25%",
                }
            }

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = side_effect
        with patch("time.sleep"):
            result = _poll_capture_completion(
                custom_api, "ns-1", "cr-1", "pattern-prog", "troshka.dev", "v1"
            )
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════════
# cancel_capture — job cancellation via troshkad
# ═══════════════════════════════════════════════════════════════════════════


class TestCancelCaptureJobs:
    @patch("app.services.troshkad_client.cancel_job")
    def test_cancels_troshkad_jobs(self, mock_cancel):
        from app.services.pattern_service import _capture_progress, cancel_capture

        _capture_progress["pat-cancel2"] = {
            "step": "capturing",
            "_host_id": "host-1",
            "_job_ids": ["j1", "j2"],
        }
        db = MagicMock()
        host = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = host
        cancel_capture("pat-cancel2", db)
        assert mock_cancel.call_count == 2

    def test_cancel_no_progress(self):
        from app.services.pattern_service import cancel_capture

        db = MagicMock()
        # Should not raise when pattern not found
        cancel_capture("pat-nonexistent", db)

    def test_cancel_no_host_or_jobs(self):
        from app.services.pattern_service import _capture_progress, cancel_capture

        _capture_progress["pat-nohost"] = {"step": "capturing"}
        db = MagicMock()
        cancel_capture("pat-nohost", db)
        # Should return without error — popped from dict
        assert "pat-nohost" not in _capture_progress
