"""Tests for uncovered functions in pattern_service.py.

Covers:
  - _capture_vm_via_nbd
  - _update_topology_with_captures
  - _restart_kubevirt_vms
  - _capture_direct
  - _run_capture_pipeline
  - capture_pattern_disks (routing + error handling)
"""

from unittest.mock import MagicMock, call, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# _update_topology_with_captures — pure function, no mocking needed
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateTopologyWithCaptures:
    def test_updates_storage_node_with_pattern_reference(self):
        from app.services.pattern_service import _update_topology_with_captures

        topo = {
            "nodes": [
                {
                    "id": "disk-1",
                    "type": "storageNode",
                    "data": {"format": "qcow2", "size": 50},
                },
            ]
        }
        captured = [{"diskId": "disk-1", "s3Key": "patterns/p1/disk-1.qcow2"}]
        _update_topology_with_captures(
            topo, captured, "pattern-abc", {"disk-1": "pd-1"}
        )

        node_data = topo["nodes"][0]["data"]
        assert node_data["source"] == "pattern"
        assert node_data["patternId"] == "pattern-abc"
        assert node_data["patternDiskId"] == "pd-1"

    def test_removes_library_item_fields(self):
        from app.services.pattern_service import _update_topology_with_captures

        topo = {
            "nodes": [
                {
                    "id": "disk-2",
                    "type": "storageNode",
                    "data": {
                        "format": "qcow2",
                        "libraryItemId": "lib-1",
                        "libraryItemName": "RHEL 9",
                    },
                },
            ]
        }
        captured = [{"diskId": "disk-2"}]
        _update_topology_with_captures(topo, captured, "p-1", {"disk-2": "pd-2"})

        assert "libraryItemId" not in topo["nodes"][0]["data"]
        assert "libraryItemName" not in topo["nodes"][0]["data"]

    def test_skips_iso_format_nodes(self):
        from app.services.pattern_service import _update_topology_with_captures

        topo = {
            "nodes": [
                {
                    "id": "iso-1",
                    "type": "storageNode",
                    "data": {"format": "iso", "size": 4},
                },
            ]
        }
        captured = [{"diskId": "iso-1"}]
        _update_topology_with_captures(topo, captured, "p-1", {})

        # ISO node should not be modified
        assert "source" not in topo["nodes"][0]["data"]

    def test_skips_non_storage_nodes(self):
        from app.services.pattern_service import _update_topology_with_captures

        topo = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"label": "master"}},
                {"id": "net-1", "type": "networkNode", "data": {"cidr": "10.0.0.0/24"}},
            ]
        }
        captured = [{"diskId": "vm-1"}, {"diskId": "net-1"}]
        _update_topology_with_captures(topo, captured, "p-1", {})

        # Neither node should have "source" added
        assert "source" not in topo["nodes"][0]["data"]
        assert "source" not in topo["nodes"][1]["data"]

    def test_no_match_leaves_node_unchanged(self):
        from app.services.pattern_service import _update_topology_with_captures

        topo = {
            "nodes": [
                {
                    "id": "disk-orphan",
                    "type": "storageNode",
                    "data": {"format": "qcow2", "size": 50},
                },
            ]
        }
        captured = [{"diskId": "disk-other"}]
        _update_topology_with_captures(topo, captured, "p-1", {})

        assert "source" not in topo["nodes"][0]["data"]

    def test_empty_topology(self):
        from app.services.pattern_service import _update_topology_with_captures

        topo = {"nodes": []}
        _update_topology_with_captures(topo, [{"diskId": "x"}], "p-1", {})
        assert topo == {"nodes": []}

    def test_multiple_storage_nodes_mixed(self):
        from app.services.pattern_service import _update_topology_with_captures

        topo = {
            "nodes": [
                {
                    "id": "d1",
                    "type": "storageNode",
                    "data": {"format": "qcow2", "size": 50},
                },
                {
                    "id": "d2",
                    "type": "storageNode",
                    "data": {"format": "iso", "size": 4},
                },
                {
                    "id": "d3",
                    "type": "storageNode",
                    "data": {"format": "qcow2", "size": 100},
                },
                {"id": "vm-1", "type": "vmNode", "data": {"label": "test"}},
            ]
        }
        captured = [{"diskId": "d1"}, {"diskId": "d3"}]
        _update_topology_with_captures(
            topo, captured, "pat-xyz", {"d1": "pd-d1", "d3": "pd-d3"}
        )

        # d1: updated
        assert topo["nodes"][0]["data"]["source"] == "pattern"
        assert topo["nodes"][0]["data"]["patternDiskId"] == "pd-d1"
        # d2: skipped (iso)
        assert "source" not in topo["nodes"][1]["data"]
        # d3: updated
        assert topo["nodes"][2]["data"]["source"] == "pattern"
        # vm-1: skipped (not storageNode)
        assert "source" not in topo["nodes"][3]["data"]

    def test_writes_pattern_disk_db_id_not_content_uuid(self):
        """patternDiskId must be the PatternDisk row id (what PatternLocation
        FKs to), NOT the content UUID / source_disk_id. Deploy placement looks
        up disk availability by PatternDisk.id, so writing the content UUID
        makes every pattern-derived deploy fail as 'not ready'."""
        from app.services.pattern_service import _update_topology_with_captures

        topo = {
            "nodes": [
                {
                    "id": "content-uuid-1",
                    "type": "storageNode",
                    "data": {"format": "qcow2", "size": 50},
                },
            ]
        }
        captured = [
            {"diskId": "content-uuid-1", "s3Key": "patterns/p/content-uuid-1.qcow2"}
        ]
        pd_id_by_disk_id = {"content-uuid-1": "pd-db-id-1"}

        _update_topology_with_captures(topo, captured, "p", pd_id_by_disk_id)

        assert topo["nodes"][0]["data"]["patternDiskId"] == "pd-db-id-1"


# ═══════════════════════════════════════════════════════════════════════════
# _restart_kubevirt_vms
# ═══════════════════════════════════════════════════════════════════════════


class TestRestartKubevirtVms:
    def test_patches_all_vms_with_running_true(self):
        from app.services.pattern_service import _restart_kubevirt_vms

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "vm-aaa"}},
                {"metadata": {"name": "vm-bbb"}},
            ]
        }
        _restart_kubevirt_vms(custom_api, "ns-test")

        assert custom_api.patch_namespaced_custom_object.call_count == 2
        # Check both VMs were patched with running=True
        calls = custom_api.patch_namespaced_custom_object.call_args_list
        for c in calls:
            assert c.kwargs["body"] == {"spec": {"running": True}}
            assert c.kwargs["namespace"] == "ns-test"
            assert c.kwargs["group"] == "kubevirt.io"

    def test_handles_empty_vm_list(self):
        from app.services.pattern_service import _restart_kubevirt_vms

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {"items": []}
        _restart_kubevirt_vms(custom_api, "ns-empty")

        custom_api.patch_namespaced_custom_object.assert_not_called()

    def test_handles_exception_gracefully(self):
        from app.services.pattern_service import _restart_kubevirt_vms

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception(
            "k8s API error"
        )
        # Should not raise
        _restart_kubevirt_vms(custom_api, "ns-broken")

    def test_handles_non_dict_response(self):
        from app.services.pattern_service import _restart_kubevirt_vms

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = "not-a-dict"
        # Non-dict triggers the isinstance fallback — items will be empty
        _restart_kubevirt_vms(custom_api, "ns-odd")
        custom_api.patch_namespaced_custom_object.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _capture_vm_via_nbd
# ═══════════════════════════════════════════════════════════════════════════


class TestCaptureVmViaNbd:
    def _make_disk_params(self):
        return [
            {
                "disk_path": "/var/lib/troshka/vms/proj/vm-disk.qcow2",
                "s3_url": "s3://bucket/patterns/pat-1/disk-1.qcow2",
                "cache_path": "/var/lib/troshka/local/cache/patterns/pat-1/disk-1.qcow2",
            }
        ]

    @patch("app.services.pattern_service._poll_job_with_progress")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_successful_capture_one_disk(self, mock_start, mock_wait, mock_poll):
        from app.services.pattern_service import _capture_vm_via_nbd

        host = MagicMock()
        host.private_ip = "10.0.0.1"
        worker = MagicMock()
        creds = {
            "access_key_id": "ak",
            "secret_access_key": "sk",
            "region": "us-east-1",
        }
        log_fn = MagicMock()

        # export -> returns port; flatten -> completed; upload -> completed;
        # stop -> completed
        mock_start.side_effect = ["export-j", "flatten-j", "upload-j", "stop-j"]
        mock_wait.side_effect = [
            {"status": "completed", "result": {"port": 10809}},  # export
            {"status": "completed"},  # stop
        ]
        mock_poll.side_effect = [
            {"status": "completed", "result": {"size_bytes": 5368709120}},  # flatten
            {"status": "completed", "result": {}},  # upload
        ]

        results = _capture_vm_via_nbd(
            host,
            worker,
            "vm-1111",
            "troshka-proj-vm11",
            self._make_disk_params(),
            creds,
            "pattern-1234",
            log_fn,
        )

        assert len(results) == 1
        assert results[0]["size_bytes"] == 5368709120
        # export start_job called with host
        assert mock_start.call_args_list[0] == call(
            host,
            "/nbd/export",
            {
                "domain_name": "troshka-proj-vm11",
                "disk_path": "/var/lib/troshka/vms/proj/vm-disk.qcow2",
            },
        )
        # flatten called on worker_host
        assert mock_start.call_args_list[1][0][0] is worker
        # upload called on worker_host
        assert mock_start.call_args_list[2][0][0] is worker
        # stop called on host
        assert mock_start.call_args_list[3][0][0] is host

    @patch("app.services.pattern_service._poll_job_with_progress")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_export_fails_raises_runtime_error(self, mock_start, mock_wait, mock_poll):
        from app.services.pattern_service import _capture_vm_via_nbd

        host = MagicMock()
        host.private_ip = "10.0.0.1"
        worker = MagicMock()
        log_fn = MagicMock()

        mock_start.side_effect = ["export-j", "stop-j"]
        mock_wait.side_effect = [
            {"status": "failed", "result": {"error": "disk not found"}},  # export fails
            {"status": "completed"},  # stop
        ]

        with pytest.raises(RuntimeError, match="NBD export failed"):
            _capture_vm_via_nbd(
                host,
                worker,
                "vm-1",
                "dom-1",
                self._make_disk_params(),
                {},
                "pat-1",
                log_fn,
            )

    @patch("app.services.pattern_service._poll_job_with_progress")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_flatten_fails_raises_runtime_error(self, mock_start, mock_wait, mock_poll):
        from app.services.pattern_service import _capture_vm_via_nbd

        host = MagicMock()
        host.private_ip = "10.0.0.1"
        worker = MagicMock()
        log_fn = MagicMock()

        mock_start.side_effect = ["export-j", "flatten-j", "stop-j"]
        mock_wait.side_effect = [
            {"status": "completed", "result": {"port": 10809}},  # export ok
            {"status": "completed"},  # stop
        ]
        mock_poll.return_value = {
            "status": "failed",
            "result": {"error": "disk I/O error"},
        }

        with pytest.raises(RuntimeError, match="Pull-flatten failed"):
            _capture_vm_via_nbd(
                host,
                worker,
                "vm-1",
                "dom-1",
                self._make_disk_params(),
                {},
                "pat-1",
                log_fn,
            )

    @patch("app.services.pattern_service._poll_job_with_progress")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_upload_fails_raises_runtime_error(self, mock_start, mock_wait, mock_poll):
        from app.services.pattern_service import _capture_vm_via_nbd

        host = MagicMock()
        host.private_ip = "10.0.0.1"
        worker = MagicMock()
        log_fn = MagicMock()

        mock_start.side_effect = ["export-j", "flatten-j", "upload-j", "stop-j"]
        mock_wait.side_effect = [
            {"status": "completed", "result": {"port": 10809}},  # export ok
            {"status": "completed"},  # stop
        ]
        mock_poll.side_effect = [
            {"status": "completed", "result": {"size_bytes": 1000}},  # flatten ok
            {
                "status": "failed",
                "result": {"error": "S3 access denied"},
            },  # upload fails
        ]

        with pytest.raises(RuntimeError, match="Upload failed"):
            _capture_vm_via_nbd(
                host,
                worker,
                "vm-1",
                "dom-1",
                self._make_disk_params(),
                {},
                "pat-1",
                log_fn,
            )

    @patch("app.services.pattern_service._poll_job_with_progress")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_finally_always_stops_nbd_export(self, mock_start, mock_wait, mock_poll):
        from app.services.pattern_service import _capture_vm_via_nbd

        host = MagicMock()
        host.private_ip = "10.0.0.1"
        worker = MagicMock()
        log_fn = MagicMock()

        mock_start.side_effect = ["export-j", "flatten-j", "stop-j"]
        mock_wait.side_effect = [
            {"status": "completed", "result": {"port": 10809}},  # export ok
            {"status": "completed"},  # stop cleanup
        ]
        mock_poll.side_effect = RuntimeError("unexpected failure")

        with pytest.raises(RuntimeError):
            _capture_vm_via_nbd(
                host,
                worker,
                "vm-1",
                "dom-1",
                self._make_disk_params(),
                {},
                "pat-1",
                log_fn,
            )

        # The stop job must always be started regardless of failures
        stop_call = mock_start.call_args_list[-1]
        assert stop_call[0][1] == "/nbd/stop"
        assert stop_call[0][2]["port"] == 10809

    @patch("app.services.pattern_service._poll_job_with_progress")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_nbd_stop_failure_is_non_fatal(self, mock_start, mock_wait, mock_poll):
        from app.services.pattern_service import _capture_vm_via_nbd

        host = MagicMock()
        host.private_ip = "10.0.0.1"
        worker = MagicMock()
        log_fn = MagicMock()

        # Successful capture, but stop fails
        mock_start.side_effect = ["export-j", "flatten-j", "upload-j", "stop-j"]
        mock_wait.side_effect = [
            {"status": "completed", "result": {"port": 10809}},  # export ok
            Exception("stop failed"),  # stop explodes
        ]
        mock_poll.side_effect = [
            {"status": "completed", "result": {"size_bytes": 1000}},  # flatten
            {"status": "completed", "result": {}},  # upload
        ]

        # Should still succeed despite stop failure
        results = _capture_vm_via_nbd(
            host,
            worker,
            "vm-1",
            "dom-1",
            self._make_disk_params(),
            {},
            "pat-1",
            log_fn,
        )
        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════
# _capture_direct
# ═══════════════════════════════════════════════════════════════════════════


class TestCaptureDirect:
    def _make_topology_parts(self):
        """Return (vm_to_disks, vm_nodes) for a single VM with one disk."""
        disk_node = {
            "id": "disk-aaa",
            "type": "storageNode",
            "data": {"format": "qcow2", "size": 50},
        }
        vm_to_disks = {"vm-111": [disk_node]}
        vm_nodes = {"vm-111": {"id": "vm-111", "data": {"label": "master"}}}
        return vm_to_disks, vm_nodes

    @patch("app.services.ws_pubsub.notify_pattern")
    @patch(
        "app.services.pattern_service._process_direct_capture_results",
        return_value=True,
    )
    @patch("app.services.troshkad_client.poll_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    @patch(
        "app.services.deploy_topology._disk_path",
        return_value="/var/lib/troshka/vms/proj/disk.qcow2",
    )
    @patch("time.sleep")
    def test_successful_capture(
        self,
        mock_sleep,
        mock_disk_path,
        mock_bucket,
        mock_start,
        mock_poll,
        mock_process,
        mock_notify,
    ):
        from app.services.pattern_service import _capture_direct

        host = MagicMock()
        host.id = "host-1234"
        db = MagicMock()
        pattern = MagicMock()
        vm_to_disks, vm_nodes = self._make_topology_parts()

        # Poll returns completed on first check
        mock_poll.return_value = {"status": "completed", "output": []}

        result = _capture_direct(
            host,
            vm_to_disks,
            vm_nodes,
            "project-1",
            "pattern-1",
            {},
            None,
            pattern,
            db,
        )

        assert result is True
        mock_start.assert_called_once()
        mock_process.assert_called_once()

    @patch("app.services.ws_pubsub.notify_pattern")
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    @patch("app.services.deploy_topology._disk_path", return_value="/path/disk.qcow2")
    def test_start_job_troshkad_error_returns_false(
        self,
        mock_disk_path,
        mock_bucket,
        mock_notify,
    ):
        from app.services.pattern_service import _capture_direct
        from app.services.troshkad_client import TroshkadError

        host = MagicMock()
        host.id = "host-1234"
        db = MagicMock()
        pattern = MagicMock()
        vm_to_disks, vm_nodes = self._make_topology_parts()

        with patch(
            "app.services.troshkad_client.start_job",
            side_effect=TroshkadError("connection refused"),
        ):
            result = _capture_direct(
                host,
                vm_to_disks,
                vm_nodes,
                "project-1",
                "pattern-1",
                {},
                None,
                pattern,
                db,
            )

        assert result is False
        assert pattern.state == "error"
        db.commit.assert_called()

    @patch("app.services.ws_pubsub.notify_pattern")
    @patch(
        "app.services.pattern_service._process_direct_capture_results",
        return_value=True,
    )
    @patch("app.services.troshkad_client.poll_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    @patch("app.services.deploy_topology._disk_path", return_value="/path/disk.qcow2")
    @patch("time.sleep")
    def test_cancelled_capture_returns_false(
        self,
        mock_sleep,
        mock_disk_path,
        mock_bucket,
        mock_start,
        mock_poll,
        mock_process,
        mock_notify,
    ):
        from app.services.pattern_service import (
            _capture_direct,
            _clear_capture_progress,
        )

        host = MagicMock()
        host.id = "host-1234"
        db = MagicMock()
        pattern = MagicMock()
        vm_to_disks, vm_nodes = self._make_topology_parts()

        # Simulate cancellation: poll returns still running, then progress removed
        poll_count = [0]

        def poll_side_effect(*args, **kwargs):
            poll_count[0] += 1
            return {"status": "running", "output": []}

        mock_poll.side_effect = poll_side_effect

        def sleep_side_effect(*args, **kwargs):
            # After first poll cycle, remove from progress (simulating cancel)
            if poll_count[0] >= 1:
                _clear_capture_progress("pattern-cancel")

        mock_sleep.side_effect = sleep_side_effect

        result = _capture_direct(
            host,
            vm_to_disks,
            vm_nodes,
            "project-1",
            "pattern-cancel",
            {},
            None,
            pattern,
            db,
        )

        assert result is False
        mock_process.assert_not_called()

    @patch("app.services.ws_pubsub.notify_pattern")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch("app.services.s3_storage._bucket", return_value="test-bucket")
    @patch("app.services.deploy_topology._disk_path", return_value="/path/disk.qcow2")
    def test_skips_iso_disks(
        self,
        mock_disk_path,
        mock_bucket,
        mock_start,
        mock_notify,
    ):
        from app.services.pattern_service import _capture_direct

        host = MagicMock()
        host.id = "host-1234"
        db = MagicMock()
        pattern = MagicMock()

        # Only ISO disks — nothing to capture
        iso_disk = {
            "id": "iso-1",
            "type": "storageNode",
            "data": {"format": "iso", "size": 4},
        }
        vm_to_disks = {"vm-1": [iso_disk]}
        vm_nodes = {"vm-1": {"id": "vm-1", "data": {"label": "vm"}}}

        with patch(
            "app.services.pattern_service._process_direct_capture_results",
            return_value=True,
        ) as _mock_proc:
            with patch("time.sleep"):
                _result = _capture_direct(
                    host,
                    vm_to_disks,
                    vm_nodes,
                    "project-1",
                    "pattern-iso",
                    {},
                    None,
                    pattern,
                    db,
                )

        # start_job should not be called (no non-ISO disks to capture)
        mock_start.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _run_capture_pipeline
# ═══════════════════════════════════════════════════════════════════════════


class TestRunCapturePipeline:
    def _make_project(self, state="active", topology=None):
        project = MagicMock()
        project.state = state
        project.id = "proj-1111"
        default_topo = {
            "nodes": [
                {"id": "vm-1", "type": "vmNode", "data": {"label": "master"}},
                {
                    "id": "disk-1",
                    "type": "storageNode",
                    "data": {"format": "qcow2", "size": 50},
                },
            ],
            "edges": [{"source": "vm-1", "target": "disk-1"}],
        }
        project.deployed_topology = topology or default_topo
        project.topology = topology or default_topo
        return project

    def _make_host(self, storage_pool_id=None):
        host = MagicMock()
        host.id = "host-1111"
        host.storage_pool_id = storage_pool_id
        return host

    @patch("app.services.pattern_service._finalize_pattern_capture")
    @patch("app.services.pattern_service._capture_container_images", return_value=True)
    @patch("app.services.pattern_service._capture_direct", return_value=True)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch(
        "app.services.s3_storage._get_s3_config", return_value={"access_key_id": "ak"}
    )
    def test_quiesces_when_active(
        self,
        mock_s3,
        mock_quiesce,
        mock_direct,
        mock_containers,
        mock_finalize,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        # No existing PatternDisk records
        db.query.return_value.filter_by.return_value.all.return_value = []
        pattern = MagicMock()
        project = self._make_project(state="active")
        host = self._make_host()

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            None,
            project,
            "proj-1",
            "pat-1",
            True,
        )

        assert result is True
        mock_quiesce.assert_called_once()

    @patch("app.services.pattern_service._finalize_pattern_capture")
    @patch("app.services.pattern_service._capture_container_images", return_value=True)
    @patch("app.services.pattern_service._capture_direct", return_value=True)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch(
        "app.services.s3_storage._get_s3_config", return_value={"access_key_id": "ak"}
    )
    def test_skips_quiesce_when_not_active(
        self,
        mock_s3,
        mock_quiesce,
        mock_direct,
        mock_containers,
        mock_finalize,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        pattern = MagicMock()
        project = self._make_project(state="stopped")
        host = self._make_host()

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            None,
            project,
            "proj-1",
            "pat-1",
            True,
        )

        assert result is True
        mock_quiesce.assert_not_called()

    @patch("app.services.pattern_service._finalize_pattern_capture")
    @patch("app.services.pattern_service._capture_container_images", return_value=True)
    @patch("app.services.pattern_service._capture_via_nbd", return_value=True)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch(
        "app.services.s3_storage._get_s3_config", return_value={"access_key_id": "ak"}
    )
    def test_delegates_to_nbd_when_worker_host_exists(
        self,
        mock_s3,
        mock_quiesce,
        mock_nbd,
        mock_containers,
        mock_finalize,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        pattern = MagicMock()
        project = self._make_project(state="stopped")
        host = self._make_host()
        worker = MagicMock()

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            worker,
            project,
            "proj-1",
            "pat-1",
            True,
        )

        assert result is True
        mock_nbd.assert_called_once()

    @patch("app.services.pattern_service._finalize_pattern_capture")
    @patch("app.services.pattern_service._capture_container_images", return_value=True)
    @patch("app.services.pattern_service._capture_direct", return_value=True)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch(
        "app.services.s3_storage._get_s3_config", return_value={"access_key_id": "ak"}
    )
    def test_delegates_to_direct_when_no_worker_host(
        self,
        mock_s3,
        mock_quiesce,
        mock_direct,
        mock_containers,
        mock_finalize,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        pattern = MagicMock()
        project = self._make_project(state="stopped")
        host = self._make_host()

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            None,
            project,
            "proj-1",
            "pat-1",
            True,
        )

        assert result is True
        mock_direct.assert_called_once()

    @patch("app.services.pattern_service._capture_container_images")
    @patch("app.services.pattern_service._capture_direct", return_value=False)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch("app.services.s3_storage._get_s3_config", return_value={})
    def test_returns_false_when_disk_capture_fails(
        self,
        mock_s3,
        mock_quiesce,
        mock_direct,
        mock_containers,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        pattern = MagicMock()
        project = self._make_project(state="stopped")
        host = self._make_host()

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            None,
            project,
            "proj-1",
            "pat-1",
            True,
        )

        assert result is False
        # Container capture should not be attempted after disk failure
        mock_containers.assert_not_called()

    @patch("app.services.pattern_service._finalize_pattern_capture")
    @patch("app.services.pattern_service._capture_container_images", return_value=False)
    @patch("app.services.pattern_service._capture_direct", return_value=True)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch("app.services.s3_storage._get_s3_config", return_value={})
    def test_returns_false_when_container_capture_fails(
        self,
        mock_s3,
        mock_quiesce,
        mock_direct,
        mock_containers,
        mock_finalize,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        pattern = MagicMock()
        project = self._make_project(state="stopped")
        host = self._make_host()

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            None,
            project,
            "proj-1",
            "pat-1",
            True,
        )

        assert result is False
        mock_finalize.assert_not_called()

    @patch("app.services.pattern_service._finalize_pattern_capture")
    @patch("app.services.pattern_service._capture_container_images", return_value=True)
    @patch("app.services.pattern_service._capture_direct", return_value=True)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch("app.services.s3_storage._get_s3_config", return_value={})
    def test_skips_already_captured_disks(
        self,
        mock_s3,
        mock_quiesce,
        mock_direct,
        mock_containers,
        mock_finalize,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        # Simulate one disk already captured
        existing_pd = MagicMock()
        existing_pd.source_disk_id = "disk-1"
        db.query.return_value.filter_by.return_value.all.return_value = [existing_pd]
        pattern = MagicMock()
        project = self._make_project(state="stopped")
        host = self._make_host()

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            None,
            project,
            "proj-1",
            "pat-1",
            True,
        )

        assert result is True
        # _capture_direct is still called (it handles per-VM skipping internally)
        mock_direct.assert_called_once()

    @patch("app.services.pattern_service._finalize_pattern_capture")
    @patch("app.services.pattern_service._capture_container_images", return_value=True)
    @patch("app.services.pattern_service._capture_direct", return_value=True)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch("app.services.s3_storage._get_s3_config", return_value={})
    def test_skips_quiesce_when_flag_is_false(
        self,
        mock_s3,
        mock_quiesce,
        mock_direct,
        mock_containers,
        mock_finalize,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        pattern = MagicMock()
        project = self._make_project(state="active")
        host = self._make_host()

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            None,
            project,
            "proj-1",
            "pat-1",
            quiesce_cluster=False,
        )

        assert result is True
        mock_quiesce.assert_not_called()

    @patch("app.services.pattern_service._finalize_pattern_capture")
    @patch("app.services.pattern_service._capture_container_images", return_value=True)
    @patch("app.services.pattern_service._capture_direct", return_value=True)
    @patch("app.services.pattern_service._quiesce_ocp_cluster")
    @patch("app.services.s3_storage._get_s3_config", return_value={})
    def test_loads_storage_pool_when_host_has_pool_id(
        self,
        mock_s3,
        mock_quiesce,
        mock_direct,
        mock_containers,
        mock_finalize,
    ):
        from app.services.pattern_service import _run_capture_pipeline

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []
        # Second query() call is for StoragePool
        mock_pool = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = mock_pool
        pattern = MagicMock()
        project = self._make_project(state="stopped")
        host = self._make_host(storage_pool_id="pool-1")

        result = _run_capture_pipeline(
            db,
            pattern,
            host,
            None,
            project,
            "proj-1",
            "pat-1",
            True,
        )

        assert result is True


# ═══════════════════════════════════════════════════════════════════════════
# capture_pattern_disks — top-level orchestrator
# ═══════════════════════════════════════════════════════════════════════════


class TestCapturePatternDisks:
    @patch("app.services.pattern_service._run_capture_pipeline")
    @patch("app.services.pattern_service._get_pattern_buffer", return_value=None)
    @patch("app.services.pattern_service.SessionLocal")
    @patch("time.sleep")
    def test_routes_normal_host(
        self,
        mock_sleep,
        mock_session_cls,
        mock_get_buffer,
        mock_pipeline,
    ):
        from app.services.pattern_service import capture_pattern_disks

        db = MagicMock()
        mock_session_cls.return_value = db

        pattern = MagicMock()
        pattern.id = "pat-1"
        project = MagicMock()
        project.id = "proj-1"
        project.host_id = "host-1"
        host = MagicMock()
        host.host_type = "ec2"

        db.query.return_value.filter_by.return_value.first.side_effect = [
            pattern,  # Pattern query
            project,  # Project query
            host,  # Host query
        ]

        capture_pattern_disks("pat-1", "proj-1")

        mock_pipeline.assert_called_once()

    @patch("app.services.pattern_service._capture_kubevirt_native")
    @patch("app.services.pattern_service.SessionLocal")
    @patch("time.sleep")
    def test_routes_to_kubevirt_for_kubevirt_cluster(
        self,
        mock_sleep,
        mock_session_cls,
        mock_kubevirt,
    ):
        from app.services.pattern_service import capture_pattern_disks

        db = MagicMock()
        mock_session_cls.return_value = db

        pattern = MagicMock()
        pattern.id = "pat-1"
        project = MagicMock()
        project.id = "proj-1"
        project.host_id = "host-kv"
        host = MagicMock()
        host.host_type = "kubevirt-cluster"

        db.query.return_value.filter_by.return_value.first.side_effect = [
            pattern,
            project,
            host,
        ]

        capture_pattern_disks("pat-1", "proj-1")

        mock_kubevirt.assert_called_once_with(db, pattern, project, host, True)

    @patch("app.services.pattern_service._mark_capture_error")
    @patch("app.services.pattern_service._run_capture_pipeline")
    @patch("app.services.pattern_service._get_pattern_buffer", return_value=None)
    @patch("app.services.pattern_service.SessionLocal")
    @patch("time.sleep")
    def test_handles_exception_marks_error(
        self,
        mock_sleep,
        mock_session_cls,
        mock_get_buffer,
        mock_pipeline,
        mock_mark_error,
    ):
        from app.services.pattern_service import capture_pattern_disks

        db = MagicMock()
        mock_session_cls.return_value = db

        pattern = MagicMock()
        pattern.id = "pat-1"
        project = MagicMock()
        project.id = "proj-1"
        project.host_id = "host-1"
        host = MagicMock()
        host.host_type = "ec2"

        db.query.return_value.filter_by.return_value.first.side_effect = [
            pattern,
            project,
            host,
        ]
        mock_pipeline.side_effect = Exception("unexpected crash")

        capture_pattern_disks("pat-1", "proj-1")

        mock_mark_error.assert_called_once_with(db, "pat-1")

    @patch("app.services.pattern_service.SessionLocal")
    @patch("time.sleep")
    def test_cleans_up_progress_on_exit(self, mock_sleep, mock_session_cls):
        from app.services.pattern_service import (
            _set_capture_progress,
            capture_pattern_disks,
            get_capture_progress,
        )

        db = MagicMock()
        mock_session_cls.return_value = db

        pattern = MagicMock()
        pattern.id = "pat-cleanup"
        project = MagicMock()
        project.id = "proj-1"
        project.host_id = "host-1"
        host = MagicMock()
        host.host_type = "ec2"

        db.query.return_value.filter_by.return_value.first.side_effect = [
            pattern,
            project,
            host,
        ]

        # Set some progress that should be cleaned up
        _set_capture_progress("pat-cleanup", {"step": "test"})

        def _finalize_capture(*args, **kwargs):
            # A successful capture ends with finalize setting step="complete";
            # only then does the finally block clear progress.
            _set_capture_progress("pat-cleanup", {"step": "complete"})
            return True

        with patch(
            "app.services.pattern_service._get_pattern_buffer", return_value=None
        ), patch(
            "app.services.pattern_service._run_capture_pipeline",
            side_effect=_finalize_capture,
        ):
            capture_pattern_disks("pat-cleanup", "proj-1")

        # Progress must be cleaned up in finally block after a successful capture
        assert get_capture_progress("pat-cleanup") is None

    @patch("app.services.pattern_service.SessionLocal")
    @patch("time.sleep")
    def test_returns_early_when_pattern_not_found(self, mock_sleep, mock_session_cls):
        from app.services.pattern_service import capture_pattern_disks

        db = MagicMock()
        mock_session_cls.return_value = db

        # Pattern not found
        db.query.return_value.filter_by.return_value.first.side_effect = [
            None,  # Pattern
            None,  # Project
        ]

        # Should return early without error
        capture_pattern_disks("pat-missing", "proj-missing")
        db.close.assert_called_once()

    @patch("app.services.pattern_service.SessionLocal")
    @patch("time.sleep")
    def test_sets_error_when_no_host_found(self, mock_sleep, mock_session_cls):
        from app.services.pattern_service import capture_pattern_disks

        db = MagicMock()
        mock_session_cls.return_value = db

        pattern = MagicMock()
        pattern.id = "pat-nohost"
        project = MagicMock()
        project.id = "proj-1"
        project.host_id = "host-gone"

        db.query.return_value.filter_by.return_value.first.side_effect = [
            pattern,  # Pattern
            project,  # Project
            None,  # Host not found
        ]

        capture_pattern_disks("pat-nohost", "proj-1")

        assert pattern.state == "error"
        db.commit.assert_called()
