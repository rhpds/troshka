"""Tests for extracted helpers in pattern_service.py."""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _quiesce_ocp_cluster
# ---------------------------------------------------------------------------
class TestQuiesceOcpCluster:
    """Tests for _quiesce_ocp_cluster."""

    def _make_ocp_topology(self, bastion_ip="10.0.0.50", password="pass"):
        return {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "bastion-1",
                    "data": {
                        "label": "bastion",
                        "nics": [{"ip": bastion_ip}],
                        "ciCloudUserPassword": password,
                    },
                },
                {
                    "type": "vmNode",
                    "id": "master-1",
                    "data": {"label": "master", "os": "rhcos"},
                },
            ],
            "edges": [],
        }

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._approve_pending_csrs", return_value=0)
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.services.deploy_service._is_ocp_topology", return_value=False)
    def test_skips_non_ocp_topology(
        self, mock_is_ocp, mock_exec, mock_csrs, mock_notify
    ):
        from app.services.pattern_service import _quiesce_ocp_cluster

        _quiesce_ocp_cluster(MagicMock(), "proj-1", {"nodes": []}, "pat-1")
        mock_csrs.assert_not_called()
        mock_exec.assert_not_called()

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._approve_pending_csrs", return_value=0)
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.services.deploy_service._is_ocp_topology", return_value=True)
    def test_skips_when_no_bastion(
        self, mock_is_ocp, mock_exec, mock_csrs, mock_notify
    ):
        from app.services.pattern_service import _quiesce_ocp_cluster

        topology = {
            "nodes": [{"type": "vmNode", "id": "m1", "data": {"label": "master"}}]
        }
        _quiesce_ocp_cluster(MagicMock(), "proj-1", topology, "pat-1")
        mock_csrs.assert_not_called()

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._approve_pending_csrs", return_value=0)
    @patch(
        "app.services.deploy_service._exec_on_bastion",
        return_value="  5 True False\n",
    )
    @patch("app.services.deploy_service._is_ocp_topology", return_value=True)
    def test_cluster_already_stable(
        self, mock_is_ocp, mock_exec, mock_csrs, mock_notify
    ):
        from app.services.pattern_service import _quiesce_ocp_cluster

        host = MagicMock()
        topo = self._make_ocp_topology()
        _quiesce_ocp_cluster(host, "proj-1", topo, "pat-1234")
        # CSRs checked at least once at start and once at end
        assert mock_csrs.call_count >= 2

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._approve_pending_csrs", return_value=3)
    @patch("app.services.deploy_service._exec_on_bastion")
    @patch("app.services.deploy_service._is_ocp_topology", return_value=True)
    def test_triggers_apiserver_rollout_when_csrs_approved(
        self, mock_is_ocp, mock_exec, mock_csrs, mock_notify
    ):
        from app.services.pattern_service import _quiesce_ocp_cluster

        # First exec call is the rollout trigger, subsequent calls are stability checks
        mock_exec.side_effect = [
            None,  # rollout patch
            "  5 True False\n",  # stability check -> all good
        ]
        host = MagicMock()
        topo = self._make_ocp_topology()
        _quiesce_ocp_cluster(host, "proj-1", topo, "pat-1234")
        # The first exec call should contain the apiserver patch
        first_exec = mock_exec.call_args_list[0]
        assert "kubeapiserver" in first_exec[0][4]

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._approve_pending_csrs", return_value=0)
    @patch(
        "app.services.deploy_service._exec_on_bastion",
        return_value="  5 True False\n",
    )
    @patch("app.services.deploy_service._is_ocp_topology", return_value=True)
    def test_uses_default_bastion_ip_when_no_nic_ip(
        self, mock_is_ocp, mock_exec, mock_csrs, mock_notify
    ):
        from app.services.pattern_service import _quiesce_ocp_cluster

        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "id": "bastion-1",
                    "data": {
                        "label": "bastion",
                        "nics": [{}],
                        "ciCloudUserPassword": "p",
                    },
                },
            ],
        }
        host = MagicMock()
        _quiesce_ocp_cluster(host, "proj-1", topo, "pat-1234")
        # Should default to 10.0.0.50
        csrs_call = mock_csrs.call_args_list[0]
        assert csrs_call[0][2] == "10.0.0.50"


# ---------------------------------------------------------------------------
# _get_pattern_buffer
# ---------------------------------------------------------------------------
class TestGetPatternBuffer:
    @patch("app.services.pattern_buffer_service.get_pattern_buffer_host")
    def test_returns_none_when_no_pool(self, mock_get_pb):
        from app.services.pattern_service import _get_pattern_buffer

        host = MagicMock()
        host.storage_pool_id = None
        result = _get_pattern_buffer(MagicMock(), host)
        assert result is None
        mock_get_pb.assert_not_called()

    @patch("app.services.pattern_buffer_service.get_pattern_buffer_host")
    def test_delegates_to_pattern_buffer_service(self, mock_get_pb):
        from app.services.pattern_service import _get_pattern_buffer

        sentinel = MagicMock()
        mock_get_pb.return_value = sentinel
        host = MagicMock()
        host.storage_pool_id = "pool-abc"
        db = MagicMock()
        result = _get_pattern_buffer(db, host)
        assert result is sentinel
        mock_get_pb.assert_called_once_with(db, "pool-abc")


# ---------------------------------------------------------------------------
# _poll_job_with_progress
# ---------------------------------------------------------------------------
class TestPollJobWithProgress:
    @patch("app.services.troshkad_client.poll_job")
    def test_returns_completed_job(self, mock_poll):
        from app.services.pattern_service import _poll_job_with_progress

        mock_poll.return_value = {
            "status": "completed",
            "output": ["Flatten done"],
            "result": {},
        }
        log_fn = MagicMock()
        host = MagicMock()
        job = _poll_job_with_progress(
            host, "job-1", log_fn, timeout=60, poll_interval=0
        )
        assert job["status"] == "completed"
        log_fn.assert_called_once_with("Flatten done")

    @patch("app.services.troshkad_client.poll_job")
    def test_returns_failed_job(self, mock_poll):
        from app.services.pattern_service import _poll_job_with_progress

        mock_poll.return_value = {"status": "failed", "output": [], "result": {}}
        log_fn = MagicMock()
        job = _poll_job_with_progress(
            MagicMock(), "job-1", log_fn, timeout=60, poll_interval=0
        )
        assert job["status"] == "failed"

    @patch("app.services.troshkad_client.poll_job")
    def test_continues_on_troshkad_error(self, mock_poll):
        from app.services.pattern_service import _poll_job_with_progress
        from app.services.troshkad_client import TroshkadError

        # First call raises, second returns completed
        mock_poll.side_effect = [
            TroshkadError("connection lost"),
            {"status": "completed", "output": [], "result": {}},
        ]
        job = _poll_job_with_progress(
            MagicMock(), "job-1", MagicMock(), timeout=60, poll_interval=0
        )
        assert job["status"] == "completed"

    @patch("app.services.troshkad_client.poll_job")
    def test_timeout_raises(self, mock_poll):
        from app.services.pattern_service import _poll_job_with_progress
        from app.services.troshkad_client import TroshkadError

        # Return running forever — the real time.time() will exceed deadline quickly
        mock_poll.return_value = {"status": "running", "output": []}
        with pytest.raises(TroshkadError, match="timed out"):
            _poll_job_with_progress(
                MagicMock(), "job-1", MagicMock(), timeout=0, poll_interval=0
            )

    @patch("app.services.troshkad_client.poll_job")
    def test_only_logs_progress_keywords(self, mock_poll):
        from app.services.pattern_service import _poll_job_with_progress

        mock_poll.side_effect = [
            {"status": "running", "output": ["Initializing...", "no keyword here"]},
            {
                "status": "completed",
                "output": ["Initializing...", "no keyword", "Upload 50%"],
            },
        ]
        log_fn = MagicMock()
        _poll_job_with_progress(
            MagicMock(), "job-1", log_fn, timeout=60, poll_interval=0
        )
        # "Upload 50%" has keyword, should be logged
        log_fn.assert_called_with("Upload 50%")


# ---------------------------------------------------------------------------
# _poll_one_capture_job
# ---------------------------------------------------------------------------
class TestPollOneCaptureJob:
    def test_already_completed(self):
        from app.services.pattern_service import _poll_one_capture_job

        jinfo = {"job_id": "j1", "vm_name": "bastion"}
        completed = {"j1"}
        result = _poll_one_capture_job(
            MagicMock(), jinfo, completed, MagicMock(), Exception
        )
        assert result == "bastion: done"

    def test_poll_error_returns_polling(self):
        from app.services.pattern_service import _poll_one_capture_job

        mock_poll = MagicMock(side_effect=ValueError("conn err"))
        jinfo = {"job_id": "j2", "vm_name": "master"}
        completed = set()
        result = _poll_one_capture_job(
            MagicMock(), jinfo, completed, mock_poll, ValueError
        )
        assert result == "master: polling..."

    def test_completed_job_added_to_set(self):
        from app.services.pattern_service import _poll_one_capture_job

        mock_poll = MagicMock(
            return_value={"status": "completed", "result": {"disks": []}, "output": []}
        )
        jinfo = {"job_id": "j3", "vm_name": "worker"}
        completed = set()
        result = _poll_one_capture_job(
            MagicMock(), jinfo, completed, mock_poll, Exception
        )
        assert result == "worker: done"
        assert "j3" in completed
        assert jinfo["_result"]["status"] == "completed"

    def test_failed_job_reports_status(self):
        from app.services.pattern_service import _poll_one_capture_job

        mock_poll = MagicMock(
            return_value={"status": "failed", "result": {}, "output": []}
        )
        jinfo = {"job_id": "j4", "vm_name": "sno"}
        completed = set()
        result = _poll_one_capture_job(
            MagicMock(), jinfo, completed, mock_poll, Exception
        )
        assert result == "sno: FAILED"
        assert "j4" in completed

    def test_running_job_extracts_progress_keyword(self):
        from app.services.pattern_service import _poll_one_capture_job

        mock_poll = MagicMock(
            return_value={
                "status": "running",
                "output": ["Starting...", "Upload 45%", "some noise"],
            }
        )
        jinfo = {"job_id": "j5", "vm_name": "infra"}
        completed = set()
        result = _poll_one_capture_job(
            MagicMock(), jinfo, completed, mock_poll, Exception
        )
        assert "Upload 45%" in result

    def test_running_job_no_keyword_shows_working(self):
        from app.services.pattern_service import _poll_one_capture_job

        mock_poll = MagicMock(
            return_value={"status": "running", "output": ["no match here"]}
        )
        jinfo = {"job_id": "j6", "vm_name": "ctrl"}
        completed = set()
        result = _poll_one_capture_job(
            MagicMock(), jinfo, completed, mock_poll, Exception
        )
        assert result == "ctrl: working..."


# ---------------------------------------------------------------------------
# _mark_capture_error
# ---------------------------------------------------------------------------
class TestMarkCaptureError:
    @patch("app.services.ws_pubsub.notify_project")
    def test_marks_pattern_error(self, mock_notify):
        from app.services.pattern_service import (
            _capture_progress,
            _mark_capture_error,
        )

        mock_pattern = MagicMock()
        mock_pattern.state = "capturing"
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = (
            mock_pattern
        )
        _mark_capture_error(mock_db, "pat-99")
        assert mock_pattern.state == "error"
        mock_db.commit.assert_called_once()
        assert _capture_progress.get("pat-99", {}).get("step") == "error"
        # cleanup
        _capture_progress.pop("pat-99", None)

    def test_swallows_exceptions(self):
        from app.services.pattern_service import _mark_capture_error

        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("db dead")
        # Should not raise
        _mark_capture_error(mock_db, "pat-404")

    @patch("app.services.ws_pubsub.notify_project")
    def test_noop_when_pattern_not_found(self, mock_notify):
        from app.services.pattern_service import _mark_capture_error

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        _mark_capture_error(mock_db, "pat-gone")
        mock_db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# _capture_container_images
# ---------------------------------------------------------------------------
class TestCaptureContainerImages:
    @patch("app.services.deploy_service._extract_containers", return_value=[])
    def test_no_containers_returns_true(self, mock_extract):
        from app.services.pattern_service import _capture_container_images

        result = _capture_container_images(
            MagicMock(), {}, "pat-1", {}, MagicMock(), MagicMock()
        )
        assert result is True

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch(
        "app.services.deploy_service._extract_containers",
        return_value=[{"node_id": "c1", "image": ""}],
    )
    def test_skips_containers_without_image(self, mock_extract, mock_start, mock_wait):
        from app.services.pattern_service import _capture_container_images

        result = _capture_container_images(
            MagicMock(), {}, "pat-1", {}, MagicMock(), MagicMock()
        )
        assert result is True
        mock_start.assert_not_called()

    @patch("app.services.s3_storage._bucket", return_value="troshka-images")
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job", return_value="job-1")
    @patch(
        "app.services.deploy_service._extract_containers",
        return_value=[{"node_id": "c1abcdef", "image": "quay.io/test:latest"}],
    )
    def test_captures_container_with_image(
        self, mock_extract, mock_start, mock_wait, mock_bucket
    ):
        from app.services.pattern_service import _capture_container_images

        result = _capture_container_images(
            MagicMock(), {}, "pat-1", {}, MagicMock(), MagicMock()
        )
        assert result is True
        # Two jobs: save-image + upload-and-cache
        assert mock_start.call_count == 2

    @patch("app.services.s3_storage._bucket", return_value="b")
    @patch("app.services.troshkad_client.start_job")
    @patch(
        "app.services.deploy_service._extract_containers",
        return_value=[{"node_id": "c1abcdef", "image": "quay.io/fail:v1"}],
    )
    def test_returns_false_on_troshkad_error(
        self, mock_extract, mock_start, mock_bucket
    ):
        from app.services.pattern_service import _capture_container_images
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("timeout")
        mock_pattern = MagicMock()
        mock_db = MagicMock()
        result = _capture_container_images(
            MagicMock(), {}, "pat-1", {}, mock_pattern, mock_db
        )
        assert result is False
        assert mock_pattern.state == "error"
        mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _finalize_pattern_capture
# ---------------------------------------------------------------------------
class TestFinalizePatternCapture:
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="troshka-images")
    @patch("app.services.pattern_service._run_recert_force_expire")
    def test_finalizes_pattern(
        self, mock_recert, mock_bucket, mock_s3_client, mock_notify
    ):
        from app.services.pattern_service import (
            _capture_progress,
            _finalize_pattern_capture,
        )

        disk1 = MagicMock()
        disk1.source_disk_id = "d1"
        disk1.id = "pd1"
        disk1.size_bytes = 1000
        disk1.source_vm_id = "vm1"
        disk1.s3_key = "patterns/pat1/d1.qcow2"
        disk1.format = "qcow2"
        disk1.virtual_size_bytes = 5000

        pattern = MagicMock()
        pattern.id = "pat-finalize"
        pattern.topology = {
            "nodes": [
                {
                    "type": "storageNode",
                    "id": "d1",
                    "data": {"format": "qcow2", "libraryItemId": "old"},
                },
                {"type": "storageNode", "id": "d2", "data": {"format": "iso"}},
            ]
        }
        pattern.disks = [disk1]
        pattern.recert = False
        pattern.name = "test-pat"
        pattern.description = "desc"
        pattern.visibility = "private"
        pattern.tags = []
        pattern.total_size_bytes = 0

        db = MagicMock()
        _finalize_pattern_capture(pattern, "pat-finalize", None, MagicMock(), db)

        assert pattern.state == "available"
        assert pattern.total_size_bytes == 1000
        db.commit.assert_called()
        # S3 metadata upload was attempted
        mock_s3_client.return_value.put_object.assert_called_once()
        # Completion notification sent
        mock_notify.assert_called()
        assert _capture_progress.get("pat-finalize", {}).get("step") == "complete"
        # cleanup
        _capture_progress.pop("pat-finalize", None)

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="b")
    @patch("app.services.pattern_service._run_recert_force_expire")
    def test_runs_recert_when_enabled(
        self, mock_recert, mock_bucket, mock_s3, mock_notify
    ):
        from app.services.pattern_service import (
            _capture_progress,
            _finalize_pattern_capture,
        )

        pattern = MagicMock()
        pattern.id = "pat-recert"
        pattern.topology = {"nodes": []}
        pattern.disks = []
        pattern.recert = True
        pattern.total_size_bytes = 0
        pattern.name = "r"
        pattern.description = ""
        pattern.visibility = "private"
        pattern.tags = []

        host = MagicMock()
        db = MagicMock()
        _finalize_pattern_capture(pattern, "pat-recert", None, host, db)

        mock_recert.assert_called_once()
        # cleanup
        _capture_progress.pop("pat-recert", None)

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._bucket", return_value="b")
    @patch("app.services.pattern_service._run_recert_force_expire")
    @patch("app.services.pattern_buffer_service.touch_activity")
    def test_touches_pattern_buffer_activity(
        self, mock_touch, mock_recert, mock_bucket, mock_s3, mock_notify
    ):
        from app.services.pattern_service import (
            _capture_progress,
            _finalize_pattern_capture,
        )

        pattern = MagicMock()
        pattern.id = "pat-pb"
        pattern.topology = {"nodes": []}
        pattern.disks = []
        pattern.recert = False
        pattern.total_size_bytes = 0
        pattern.name = "pb"
        pattern.description = ""
        pattern.visibility = "private"
        pattern.tags = []

        worker = MagicMock()
        worker.storage_pool_id = "pool-123"
        db = MagicMock()
        _finalize_pattern_capture(pattern, "pat-pb", worker, MagicMock(), db)
        mock_touch.assert_called_once_with(db, "pool-123")
        # cleanup
        _capture_progress.pop("pat-pb", None)


# ---------------------------------------------------------------------------
# _build_disk_to_vm_map
# ---------------------------------------------------------------------------
class TestBuildDiskToVmMap:
    def test_empty_topology(self):
        from app.services.pattern_service import _build_disk_to_vm_map

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map({})
        assert disk_nodes == []
        assert vm_nodes == {}
        assert disk_to_vm == {}
        assert vm_to_disks == {}

    def test_single_vm_single_disk_edge_vm_to_disk(self):
        from app.services.pattern_service import _build_disk_to_vm_map

        topology = {
            "nodes": [
                {"type": "vmNode", "id": "vm-1", "data": {"label": "master"}},
                {"type": "storageNode", "id": "disk-1", "data": {"format": "qcow2"}},
            ],
            "edges": [{"source": "vm-1", "target": "disk-1"}],
        }
        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)
        assert len(disk_nodes) == 1
        assert disk_nodes[0]["id"] == "disk-1"
        assert "vm-1" in vm_nodes
        assert disk_to_vm == {"disk-1": "vm-1"}
        assert vm_to_disks == {"vm-1": [disk_nodes[0]]}

    def test_edge_disk_to_vm_direction(self):
        from app.services.pattern_service import _build_disk_to_vm_map

        topology = {
            "nodes": [
                {"type": "vmNode", "id": "vm-1", "data": {}},
                {"type": "storageNode", "id": "disk-1", "data": {}},
            ],
            "edges": [{"source": "disk-1", "target": "vm-1"}],
        }
        _, _, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)
        assert disk_to_vm == {"disk-1": "vm-1"}
        assert "vm-1" in vm_to_disks

    def test_unconnected_disk_excluded(self):
        from app.services.pattern_service import _build_disk_to_vm_map

        topology = {
            "nodes": [
                {"type": "vmNode", "id": "vm-1", "data": {}},
                {"type": "storageNode", "id": "disk-1", "data": {}},
                {"type": "storageNode", "id": "disk-orphan", "data": {}},
            ],
            "edges": [{"source": "vm-1", "target": "disk-1"}],
        }
        disk_nodes, _, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)
        assert len(disk_nodes) == 2  # both listed
        assert "disk-orphan" not in disk_to_vm  # orphan not mapped
        assert len(vm_to_disks["vm-1"]) == 1

    def test_multiple_vms_multiple_disks(self):
        from app.services.pattern_service import _build_disk_to_vm_map

        topology = {
            "nodes": [
                {"type": "vmNode", "id": "vm-a", "data": {}},
                {"type": "vmNode", "id": "vm-b", "data": {}},
                {"type": "storageNode", "id": "d1", "data": {}},
                {"type": "storageNode", "id": "d2", "data": {}},
                {"type": "storageNode", "id": "d3", "data": {}},
                {"type": "networkNode", "id": "net-1", "data": {}},
            ],
            "edges": [
                {"source": "vm-a", "target": "d1"},
                {"source": "d2", "target": "vm-a"},
                {"source": "vm-b", "target": "d3"},
            ],
        }
        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)
        assert len(disk_nodes) == 3
        assert len(vm_nodes) == 2
        assert disk_to_vm == {"d1": "vm-a", "d2": "vm-a", "d3": "vm-b"}
        assert len(vm_to_disks["vm-a"]) == 2
        assert len(vm_to_disks["vm-b"]) == 1


# ---------------------------------------------------------------------------
# _build_nbd_vm_tasks
# ---------------------------------------------------------------------------
class TestBuildNbdVmTasks:
    @patch("app.services.s3_storage._bucket", return_value="my-bucket")
    @patch(
        "app.services.deploy_service._disk_path",
        return_value="/var/lib/troshka/vms/proj/vm-disk.qcow2",
    )
    def test_single_vm_single_disk(self, mock_disk_path, mock_bucket):
        from app.services.pattern_service import _build_nbd_vm_tasks

        vm_to_disks = {
            "vm-1234abcd": [
                {"id": "d-aabbccdd", "data": {"format": "qcow2", "size": 20}},
            ]
        }
        vm_nodes = {"vm-1234abcd": {"id": "vm-1234abcd", "data": {"label": "master"}}}
        tasks = _build_nbd_vm_tasks(
            vm_to_disks, vm_nodes, "proj-1234", "pat-5678", None
        )
        assert len(tasks) == 1
        task = tasks[0]
        assert task["vm_id"] == "vm-1234abcd"
        assert task["vm_name"] == "master"
        assert task["domain_name"] == "troshka-proj-123-vm-1234a"
        assert len(task["disks_params"]) == 1
        assert (
            task["disks_params"][0]["s3_url"]
            == "s3://my-bucket/patterns/pat-5678/d-aabbccdd.qcow2"
        )
        assert task["disks_params"][0]["virtual_size_bytes"] == 20 * 1073741824
        assert task["disk_metadata"][0]["disk_id"] == "d-aabbccdd"
        assert task["disk_metadata"][0]["format"] == "qcow2"

    @patch("app.services.s3_storage._bucket", return_value="bucket")
    @patch("app.services.deploy_service._disk_path", return_value="/path/disk")
    def test_skips_iso_disks(self, mock_dp, mock_bucket):
        from app.services.pattern_service import _build_nbd_vm_tasks

        vm_to_disks = {
            "vm-1": [
                {"id": "d-iso", "data": {"format": "iso"}},
            ]
        }
        vm_nodes = {"vm-1": {"id": "vm-1", "data": {"label": "worker"}}}
        tasks = _build_nbd_vm_tasks(vm_to_disks, vm_nodes, "proj", "pat", None)
        assert tasks == []

    @patch("app.services.s3_storage._bucket", return_value="bucket")
    @patch("app.services.deploy_service._disk_path", return_value="/path/disk")
    def test_mixed_iso_and_qcow2(self, mock_dp, mock_bucket):
        from app.services.pattern_service import _build_nbd_vm_tasks

        vm_to_disks = {
            "vm-1": [
                {"id": "d-iso", "data": {"format": "iso"}},
                {"id": "d-qcow", "data": {"format": "qcow2", "size": 10}},
            ]
        }
        vm_nodes = {"vm-1": {"id": "vm-1", "data": {"label": "sno"}}}
        tasks = _build_nbd_vm_tasks(vm_to_disks, vm_nodes, "proj", "pat", None)
        assert len(tasks) == 1
        assert len(tasks[0]["disks_params"]) == 1
        assert tasks[0]["disk_metadata"][0]["disk_id"] == "d-qcow"

    @patch("app.services.s3_storage._bucket", return_value="bucket")
    @patch("app.services.deploy_service._disk_path", return_value="/path/disk")
    def test_uses_label_fallback_to_truncated_id(self, mock_dp, mock_bucket):
        from app.services.pattern_service import _build_nbd_vm_tasks

        vm_to_disks = {
            "vm-longid12": [
                {"id": "d1", "data": {"format": "qcow2", "size": 5}},
            ]
        }
        # VM node with no data.label
        vm_nodes = {"vm-longid12": {"id": "vm-longid12", "data": {}}}
        tasks = _build_nbd_vm_tasks(vm_to_disks, vm_nodes, "proj", "pat", None)
        assert tasks[0]["vm_name"] == "vm-longi"  # id[:8]

    @patch("app.services.s3_storage._bucket", return_value="bucket")
    @patch("app.services.deploy_service._disk_path", return_value="/path/disk")
    def test_default_format_is_qcow2(self, mock_dp, mock_bucket):
        from app.services.pattern_service import _build_nbd_vm_tasks

        vm_to_disks = {"vm-1": [{"id": "d1", "data": {}}]}  # no format key
        vm_nodes = {"vm-1": {"id": "vm-1", "data": {"label": "test"}}}
        tasks = _build_nbd_vm_tasks(vm_to_disks, vm_nodes, "proj", "pat", None)
        assert len(tasks) == 1
        assert tasks[0]["disk_metadata"][0]["format"] == "qcow2"


# ---------------------------------------------------------------------------
# _resolve_job_result
# ---------------------------------------------------------------------------
class TestResolveJobResult:
    def test_returns_cached_result(self):
        from app.services.pattern_service import _resolve_job_result

        cached = {"status": "completed", "result": {"disks": []}}
        jinfo = {"job_id": "j1", "_result": cached}
        result = _resolve_job_result(jinfo, MagicMock())
        assert result is cached

    @patch("app.services.troshkad_client.poll_job")
    def test_polls_when_no_cached_result(self, mock_poll):
        from app.services.pattern_service import _resolve_job_result

        polled = {"status": "completed", "result": {"disks": [{"size_bytes": 100}]}}
        mock_poll.return_value = polled
        jinfo = {"job_id": "j2"}
        host = MagicMock()
        result = _resolve_job_result(jinfo, host)
        assert result is polled
        mock_poll.assert_called_once_with(host, "j2")

    @patch("app.services.troshkad_client.poll_job")
    def test_returns_failed_on_troshkad_error(self, mock_poll):
        from app.services.pattern_service import _resolve_job_result
        from app.services.troshkad_client import TroshkadError

        mock_poll.side_effect = TroshkadError("connection refused")
        jinfo = {"job_id": "j3"}
        result = _resolve_job_result(jinfo, MagicMock())
        assert result["status"] == "failed"
        assert "Job lost" in result["result"]["error"]

    @patch("app.services.troshkad_client.poll_job")
    def test_returns_failed_when_poll_returns_none(self, mock_poll):
        from app.services.pattern_service import _resolve_job_result

        mock_poll.return_value = None
        jinfo = {"job_id": "j4"}
        result = _resolve_job_result(jinfo, MagicMock())
        assert result["status"] == "failed"
        assert "missing" in result["result"]["error"]


# ---------------------------------------------------------------------------
# _save_vm_disks
# ---------------------------------------------------------------------------
class TestSaveVmDisks:
    @patch("app.services.pattern_service.PatternDisk")
    def test_creates_pattern_disk_records(self, mock_pd_cls):
        from app.services.pattern_service import _save_vm_disks

        job = {
            "status": "completed",
            "result": {
                "disks": [
                    {"size_bytes": 5000},
                    {"size_bytes": 12000},
                ]
            },
        }
        jinfo = {
            "disk_metadata": [
                {
                    "disk_id": "d1",
                    "vm_id": "vm-a",
                    "s3_key": "patterns/pat/d1.qcow2",
                    "format": "qcow2",
                    "virtual_size_bytes": 20000,
                },
                {
                    "disk_id": "d2",
                    "vm_id": "vm-a",
                    "s3_key": "patterns/pat/d2.qcow2",
                    "format": "qcow2",
                    "virtual_size_bytes": 40000,
                },
            ]
        }
        db = MagicMock()
        _save_vm_disks(job, jinfo, "pat-1", db)
        assert mock_pd_cls.call_count == 2
        assert db.add.call_count == 2
        db.commit.assert_called_once()
        # Verify first disk got the right size_bytes
        first_call_kwargs = mock_pd_cls.call_args_list[0][1]
        assert first_call_kwargs["size_bytes"] == 5000
        assert first_call_kwargs["source_disk_id"] == "d1"
        assert first_call_kwargs["pattern_id"] == "pat-1"

    @patch("app.services.pattern_service.PatternDisk")
    def test_size_zero_when_results_shorter_than_metadata(self, mock_pd_cls):
        from app.services.pattern_service import _save_vm_disks

        job = {
            "result": {
                "disks": [{"size_bytes": 100}]
                # only 1 result but 2 metadata entries
            }
        }
        jinfo = {
            "disk_metadata": [
                {
                    "disk_id": "d1",
                    "vm_id": "vm-1",
                    "s3_key": "s3/d1",
                    "format": "qcow2",
                    "virtual_size_bytes": 1000,
                },
                {
                    "disk_id": "d2",
                    "vm_id": "vm-1",
                    "s3_key": "s3/d2",
                    "format": "qcow2",
                    "virtual_size_bytes": 2000,
                },
            ]
        }
        db = MagicMock()
        _save_vm_disks(job, jinfo, "pat-x", db)
        second_call_kwargs = mock_pd_cls.call_args_list[1][1]
        assert second_call_kwargs["size_bytes"] == 0

    @patch("app.services.pattern_service.PatternDisk")
    def test_handles_none_job(self, mock_pd_cls):
        from app.services.pattern_service import _save_vm_disks

        jinfo = {
            "disk_metadata": [
                {
                    "disk_id": "d1",
                    "vm_id": "vm-1",
                    "s3_key": "s3/d1",
                    "format": "qcow2",
                    "virtual_size_bytes": 1000,
                },
            ]
        }
        db = MagicMock()
        _save_vm_disks(None, jinfo, "pat-n", db)
        call_kwargs = mock_pd_cls.call_args_list[0][1]
        assert call_kwargs["size_bytes"] == 0
        db.commit.assert_called_once()

    @patch("app.services.pattern_service.PatternDisk")
    def test_empty_disk_metadata(self, mock_pd_cls):
        from app.services.pattern_service import _save_vm_disks

        job = {"result": {"disks": []}}
        jinfo = {"disk_metadata": []}
        db = MagicMock()
        _save_vm_disks(job, jinfo, "pat-e", db)
        mock_pd_cls.assert_not_called()
        db.add.assert_not_called()
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _process_direct_capture_results
# ---------------------------------------------------------------------------
class TestProcessDirectCaptureResults:
    @patch("app.services.pattern_service._save_vm_disks")
    @patch("app.services.pattern_service._resolve_job_result")
    def test_all_succeed(self, mock_resolve, mock_save):
        from app.services.pattern_service import _process_direct_capture_results

        mock_resolve.return_value = {
            "status": "completed",
            "result": {"disks": [{"size_bytes": 100}]},
        }
        all_jobs = [
            {
                "job_id": "j1",
                "vm_id": "vm-1111",
                "vm_name": "master",
                "disk_metadata": [],
            },
            {
                "job_id": "j2",
                "vm_id": "vm-2222",
                "vm_name": "worker",
                "disk_metadata": [],
            },
        ]
        pattern = MagicMock()
        db = MagicMock()
        result = _process_direct_capture_results(
            all_jobs, MagicMock(), "pat-ok", pattern, db
        )
        assert result is True
        assert mock_save.call_count == 2

    @patch("app.services.pattern_service._save_vm_disks")
    @patch("app.services.pattern_service._resolve_job_result")
    def test_failed_job_sets_error(self, mock_resolve, mock_save):
        from app.services.pattern_service import _process_direct_capture_results

        mock_resolve.return_value = {
            "status": "failed",
            "result": {"error": "disk full"},
        }
        all_jobs = [
            {
                "job_id": "j1",
                "vm_id": "vm-1111",
                "vm_name": "bastion",
                "disk_metadata": [],
            },
        ]
        pattern = MagicMock()
        db = MagicMock()
        result = _process_direct_capture_results(
            all_jobs, MagicMock(), "pat-fail", pattern, db
        )
        assert result is False
        assert pattern.state == "error"
        db.commit.assert_called()
        mock_save.assert_not_called()

    @patch("app.services.pattern_service._save_vm_disks")
    @patch("app.services.pattern_service._resolve_job_result")
    def test_mixed_success_and_failure(self, mock_resolve, mock_save):
        from app.services.pattern_service import _process_direct_capture_results

        mock_resolve.side_effect = [
            {"status": "completed", "result": {"disks": []}},
            {"status": "failed", "result": {"error": "timeout"}},
        ]
        all_jobs = [
            {
                "job_id": "j1",
                "vm_id": "vm-aaaa",
                "vm_name": "ok-vm",
                "disk_metadata": [],
            },
            {
                "job_id": "j2",
                "vm_id": "vm-bbbb",
                "vm_name": "bad-vm",
                "disk_metadata": [],
            },
        ]
        pattern = MagicMock()
        db = MagicMock()
        result = _process_direct_capture_results(
            all_jobs, MagicMock(), "pat-mix", pattern, db
        )
        assert result is False
        assert pattern.state == "error"
        # The successful one was saved, the failed one was not
        assert mock_save.call_count == 1

    @patch("app.services.pattern_service._save_vm_disks")
    @patch("app.services.pattern_service._resolve_job_result")
    def test_troshkad_error_during_save(self, mock_resolve, mock_save):
        from app.services.pattern_service import _process_direct_capture_results
        from app.services.troshkad_client import TroshkadError

        mock_resolve.return_value = {
            "status": "completed",
            "result": {"disks": []},
        }
        mock_save.side_effect = TroshkadError("connection lost")
        all_jobs = [
            {"job_id": "j1", "vm_id": "vm-cccc", "vm_name": "sno", "disk_metadata": []},
        ]
        pattern = MagicMock()
        db = MagicMock()
        result = _process_direct_capture_results(
            all_jobs, MagicMock(), "pat-err", pattern, db
        )
        assert result is False
        assert pattern.state == "error"

    @patch("app.services.pattern_service._save_vm_disks")
    @patch("app.services.pattern_service._resolve_job_result")
    def test_empty_jobs_returns_true(self, mock_resolve, mock_save):
        from app.services.pattern_service import _process_direct_capture_results

        result = _process_direct_capture_results(
            [], MagicMock(), "pat-empty", MagicMock(), MagicMock()
        )
        assert result is True
        mock_resolve.assert_not_called()
        mock_save.assert_not_called()


# ── get_capture_progress tests ──


class TestGetCaptureProgress:
    def test_returns_progress_when_exists(self):
        from app.services.pattern_service import _capture_progress, get_capture_progress

        _capture_progress["pat-test"] = {"percent": 50, "step": "uploading"}
        try:
            result = get_capture_progress("pat-test")
            assert result["percent"] == 50
            assert result["step"] == "uploading"
        finally:
            _capture_progress.pop("pat-test", None)

    def test_returns_none_when_not_exists(self):
        from app.services.pattern_service import _capture_progress, get_capture_progress

        _capture_progress.pop("pat-missing", None)
        result = get_capture_progress("pat-missing")
        assert result is None


# ── cancel_capture tests ──


class TestCancelCapture:
    def test_cancel_capture_cancels_jobs(self):
        from app.services.pattern_service import _capture_progress, cancel_capture

        host = MagicMock()
        host.id = "host-1"

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = host

        _capture_progress["pat-1"] = {
            "_host_id": "host-1",
            "_job_ids": ["job-a", "job-b"],
        }
        try:
            with patch("app.services.troshkad_client.cancel_job") as mock_cancel:
                cancel_capture("pat-1", mock_db)
                assert mock_cancel.call_count == 2
        finally:
            _capture_progress.pop("pat-1", None)

    def test_cancel_capture_no_progress(self):
        from app.services.pattern_service import _capture_progress, cancel_capture

        _capture_progress.pop("pat-missing", None)
        mock_db = MagicMock()

        # Should not raise
        cancel_capture("pat-missing", mock_db)

    def test_cancel_capture_no_host_id(self):
        from app.services.pattern_service import _capture_progress, cancel_capture

        _capture_progress["pat-2"] = {"_host_id": None, "_job_ids": ["j1"]}
        try:
            mock_db = MagicMock()
            cancel_capture("pat-2", mock_db)
            # Should return early without querying host
            mock_db.query.assert_not_called()
        finally:
            _capture_progress.pop("pat-2", None)


# ── _run_recert_force_expire tests ──


class TestRunRecertForceExpire:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_runs_recert_on_single_rhcos_vm(self, mock_start, mock_wait):
        from app.services.pattern_service import _run_recert_force_expire

        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}

        host = MagicMock()
        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"os": "rhcos"}},
            ]
        }
        disk = MagicMock()
        disk.source_vm_id = "vm1"
        disk.format = "qcow2"
        disk.id = "disk-1"

        mock_db = MagicMock()
        _run_recert_force_expire(host, "pat-1", topology, [disk], mock_db)
        mock_start.assert_called_once()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_skips_when_multiple_rhcos_vms(self, mock_start, mock_wait):
        from app.services.pattern_service import _run_recert_force_expire

        host = MagicMock()
        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"os": "rhcos"}},
                {"id": "vm2", "type": "vmNode", "data": {"os": "rhcos"}},
            ]
        }
        mock_db = MagicMock()
        _run_recert_force_expire(host, "pat-1", topology, [], mock_db)
        mock_start.assert_not_called()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_skips_when_no_rhcos_disk(self, mock_start, mock_wait):
        from app.services.pattern_service import _run_recert_force_expire

        host = MagicMock()
        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"os": "rhcos"}},
            ]
        }
        disk = MagicMock()
        disk.source_vm_id = "vm2"  # different VM
        disk.format = "qcow2"

        mock_db = MagicMock()
        _run_recert_force_expire(host, "pat-1", topology, [disk], mock_db)
        mock_start.assert_not_called()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_clears_recert_on_failure(self, mock_start, mock_wait):
        from app.services.pattern_service import _run_recert_force_expire

        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "failed", "result": {"error": "boom"}}

        host = MagicMock()
        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"os": "rhcos"}},
            ]
        }
        disk = MagicMock()
        disk.source_vm_id = "vm1"
        disk.format = "qcow2"
        disk.id = "disk-1"

        pat_mock = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = pat_mock

        _run_recert_force_expire(host, "pat-1", topology, [disk], mock_db)
        assert pat_mock.recert is False

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_clears_recert_on_exception(self, mock_start, mock_wait):
        from app.services.pattern_service import _run_recert_force_expire

        mock_start.side_effect = Exception("connection error")

        host = MagicMock()
        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"os": "rhcos"}},
            ]
        }
        disk = MagicMock()
        disk.source_vm_id = "vm1"
        disk.format = "qcow2"
        disk.id = "disk-1"

        pat_mock = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = pat_mock

        _run_recert_force_expire(host, "pat-1", topology, [disk], mock_db)
        assert pat_mock.recert is False


# ── capture_pattern_disks entry point tests ──


class TestCapturePatternDisks:
    @patch("time.sleep")
    @patch("app.services.pattern_service.SessionLocal")
    def test_capture_pattern_not_found(self, mock_session_cls, mock_sleep):
        from app.services.pattern_service import capture_pattern_disks

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_session_cls.return_value = mock_db

        # Should not raise, just log and return
        capture_pattern_disks("pat-missing", "proj-missing")

    @patch("time.sleep")
    @patch("app.services.pattern_service.SessionLocal")
    def test_capture_no_host_marks_error(self, mock_session_cls, mock_sleep):
        from app.services.pattern_service import (
            _capture_progress,
            capture_pattern_disks,
        )

        pattern = MagicMock()
        pattern.id = "pat-1"
        project = MagicMock()
        project.id = "proj-1"
        project.host_id = "host-1"

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.side_effect = [
            pattern,  # pattern query
            project,  # project query
            None,  # host query
        ]
        mock_session_cls.return_value = mock_db

        capture_pattern_disks("pat-1", "proj-1")
        assert pattern.state == "error"
        _capture_progress.pop("pat-1", None)


# ---------------------------------------------------------------------------
# Additional coverage for _run_recert_force_expire
# ---------------------------------------------------------------------------
class TestRunRecertForceExpireExtra:
    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_skips_when_zero_rhcos_vms(self, mock_start, mock_wait):
        """Covers the len(rhcos_vms) != 1 branch when there are 0 RHCOS VMs."""
        from app.services.pattern_service import _run_recert_force_expire

        host = MagicMock()
        topology = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"os": "rhel9"}},
                {"id": "vm2", "type": "vmNode", "data": {"os": "windows"}},
            ]
        }
        mock_db = MagicMock()
        _run_recert_force_expire(host, "pat-zero", topology, [], mock_db)
        mock_start.assert_not_called()
        mock_wait.assert_not_called()

    @patch("app.services.troshkad_client.wait_for_job")
    @patch("app.services.troshkad_client.start_job")
    def test_skips_when_no_vm_nodes_at_all(self, mock_start, mock_wait):
        """Covers the branch when topology has no vmNode entries."""
        from app.services.pattern_service import _run_recert_force_expire

        host = MagicMock()
        topology = {"nodes": [{"id": "net1", "type": "networkNode", "data": {}}]}
        mock_db = MagicMock()
        _run_recert_force_expire(host, "pat-novms", topology, [], mock_db)
        mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# Additional coverage for _poll_job_with_progress
# ---------------------------------------------------------------------------
class TestPollJobWithProgressExtra:
    @patch("app.services.troshkad_client.poll_job")
    def test_forwards_cach_keyword(self, mock_poll):
        """Covers the 'Cach' keyword branch in output forwarding."""
        from app.services.pattern_service import _poll_job_with_progress

        mock_poll.return_value = {
            "status": "completed",
            "output": ["Caching pattern to local disk"],
        }
        log_fn = MagicMock()
        job = _poll_job_with_progress(
            MagicMock(), "job-cache", log_fn, timeout=60, poll_interval=0
        )
        assert job["status"] == "completed"
        log_fn.assert_called_once_with("Caching pattern to local disk")

    @patch("app.services.troshkad_client.poll_job")
    def test_does_not_forward_non_keyword_output(self, mock_poll):
        """Ensures lines without Flatten/Upload/Cach are NOT forwarded."""
        from app.services.pattern_service import _poll_job_with_progress

        mock_poll.side_effect = [
            {"status": "running", "output": ["Starting job...", "Reading disk"]},
            {"status": "completed", "output": ["Starting job...", "Reading disk"]},
        ]
        log_fn = MagicMock()
        _poll_job_with_progress(
            MagicMock(), "job-no-kw", log_fn, timeout=60, poll_interval=0
        )
        log_fn.assert_not_called()

    @patch("app.services.troshkad_client.poll_job")
    def test_tracks_output_length_across_polls(self, mock_poll):
        """Ensures last_output_len tracks correctly, only new lines trigger keyword check."""
        from app.services.pattern_service import _poll_job_with_progress

        mock_poll.side_effect = [
            {"status": "running", "output": ["line1"]},
            {"status": "running", "output": ["line1", "Flatten 30%"]},
            {"status": "completed", "output": ["line1", "Flatten 30%", "Upload 100%"]},
        ]
        log_fn = MagicMock()
        _poll_job_with_progress(
            MagicMock(), "job-track", log_fn, timeout=60, poll_interval=0
        )
        assert log_fn.call_count == 2
        log_fn.assert_any_call("Flatten 30%")
        log_fn.assert_any_call("Upload 100%")


# ---------------------------------------------------------------------------
# Additional coverage for _get_pattern_buffer
# ---------------------------------------------------------------------------
class TestGetPatternBufferExtra:
    @patch(
        "app.services.pattern_buffer_service.get_pattern_buffer_host", return_value=None
    )
    def test_returns_none_when_service_returns_none(self, mock_get_pb):
        """Covers the case where pool exists but no buffer host is available."""
        from app.services.pattern_service import _get_pattern_buffer

        host = MagicMock()
        host.storage_pool_id = "pool-xyz"
        db = MagicMock()
        result = _get_pattern_buffer(db, host)
        assert result is None
        mock_get_pb.assert_called_once_with(db, "pool-xyz")
