"""Tests for extracted helper functions in app.api.projects."""

import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test_proj_svc_helpers.db"

import datetime
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _build_redeploy_vm_data
# ---------------------------------------------------------------------------


class TestBuildRedeployVmData(unittest.TestCase):
    def _call(self, vm_node):
        from app.api.projects import _build_redeploy_vm_data

        return _build_redeploy_vm_data(vm_node)

    def test_full_data(self):
        node = {
            "id": "vm-1",
            "data": {
                "name": "bastion",
                "vcpus": 4,
                "ram": 16,
                "cloudInit": True,
                "bootDevices": ["disk-1"],
                "firmware": "uefi",
                "secureBoot": True,
            },
        }
        result = self._call(node)
        assert result["node_id"] == "vm-1"
        assert result["name"] == "bastion"
        assert result["vcpus"] == 4
        assert result["ram_gb"] == 16
        assert result["cloud_init"] is True
        assert result["boot_devices"] == ["disk-1"]
        assert result["firmware"] == "uefi"
        assert result["secure_boot"] is True

    def test_defaults_applied(self):
        node = {"id": "vm-2", "data": {}}
        result = self._call(node)
        assert result["node_id"] == "vm-2"
        assert result["name"] == "vm"
        assert result["vcpus"] == 2
        assert result["ram_gb"] == 4
        assert result["cloud_init"] is False
        assert result["boot_devices"] is None
        assert result["firmware"] == "bios"
        assert result["secure_boot"] is False

    def test_missing_data_key(self):
        node = {"id": "vm-3"}
        result = self._call(node)
        assert result["node_id"] == "vm-3"
        assert result["name"] == "vm"
        assert result["vcpus"] == 2
        assert result["ram_gb"] == 4

    def test_partial_data(self):
        node = {"id": "vm-4", "data": {"name": "worker", "vcpus": 8}}
        result = self._call(node)
        assert result["name"] == "worker"
        assert result["vcpus"] == 8
        assert result["ram_gb"] == 4  # default
        assert result["firmware"] == "bios"  # default


# ---------------------------------------------------------------------------
# _recompute_auto_stop_timer
# ---------------------------------------------------------------------------


class TestRecomputeAutoStopTimer(unittest.TestCase):
    def _call(self, project, fields):
        from app.api.projects import _recompute_auto_stop_timer

        return _recompute_auto_stop_timer(project, fields)

    def test_none_clears_fields(self):
        project = MagicMock()
        project.auto_stop_started_at = datetime.datetime(
            2025, 1, 1, tzinfo=datetime.UTC
        )
        project.auto_stop_expires_at = datetime.datetime(
            2025, 1, 1, 1, 0, tzinfo=datetime.UTC
        )
        project.auto_stop_warned = True

        self._call(project, {"auto_stop_minutes": None})

        assert project.auto_stop_started_at is None
        assert project.auto_stop_expires_at is None
        assert project.auto_stop_warned is False

    def test_active_project_stamps_now(self):
        project = MagicMock()
        project.auto_stop_started_at = None
        project.state = "active"
        project.auto_stop_minutes = 60

        self._call(project, {"auto_stop_minutes": 60})

        assert isinstance(project.auto_stop_started_at, datetime.datetime)
        assert isinstance(project.auto_stop_expires_at, datetime.datetime)
        delta = project.auto_stop_expires_at - project.auto_stop_started_at
        assert abs(delta.total_seconds() - 3600) < 2

    def test_existing_start_recomputes_expiry(self):
        existing_start = datetime.datetime(2025, 1, 1, 0, 0, tzinfo=datetime.UTC)
        project = MagicMock()
        project.auto_stop_started_at = existing_start
        project.state = "active"
        project.auto_stop_minutes = 30

        self._call(project, {"auto_stop_minutes": 30})

        assert project.auto_stop_started_at == existing_start
        expected = existing_start + datetime.timedelta(minutes=30)
        assert project.auto_stop_expires_at == expected
        assert project.auto_stop_warned is False

    def test_non_active_does_not_stamp_start(self):
        project = MagicMock()
        project.auto_stop_started_at = None
        project.state = "stopped"
        project.auto_stop_minutes = 120
        sentinel = object()
        project.auto_stop_expires_at = sentinel

        self._call(project, {"auto_stop_minutes": 120})

        # started_at should remain None (project is not "active")
        assert project.auto_stop_started_at is None
        # expires_at should not have been reassigned
        assert project.auto_stop_expires_at is sentinel
        assert project.auto_stop_warned is False


# ---------------------------------------------------------------------------
# _recompute_auto_delete_timer
# ---------------------------------------------------------------------------


class TestRecomputeAutoDeleteTimer(unittest.TestCase):
    def _call(self, project, fields):
        from app.api.projects import _recompute_auto_delete_timer

        return _recompute_auto_delete_timer(project, fields)

    def test_none_clears_fields(self):
        project = MagicMock()
        project.auto_delete_started_at = datetime.datetime(
            2025, 6, 1, tzinfo=datetime.UTC
        )
        project.lifetime_expires_at = datetime.datetime(2025, 6, 2, tzinfo=datetime.UTC)
        project.auto_delete_warned = True

        self._call(project, {"auto_delete_minutes": None})

        assert project.auto_delete_started_at is None
        assert project.lifetime_expires_at is None
        assert project.auto_delete_warned is False

    def test_non_draft_stamps_now(self):
        project = MagicMock()
        project.auto_delete_started_at = None
        project.state = "active"
        project.auto_delete_minutes = 1440

        self._call(project, {"auto_delete_minutes": 1440})

        assert isinstance(project.auto_delete_started_at, datetime.datetime)
        assert isinstance(project.lifetime_expires_at, datetime.datetime)
        delta = project.lifetime_expires_at - project.auto_delete_started_at
        assert abs(delta.total_seconds() - 1440 * 60) < 2

    def test_draft_does_not_stamp_start(self):
        project = MagicMock()
        project.auto_delete_started_at = None
        project.state = "draft"
        project.auto_delete_minutes = 120
        sentinel = object()
        project.lifetime_expires_at = sentinel

        self._call(project, {"auto_delete_minutes": 120})

        assert project.auto_delete_started_at is None
        assert project.lifetime_expires_at is sentinel

    def test_existing_start_recomputes(self):
        existing_start = datetime.datetime(2025, 3, 1, 12, 0, tzinfo=datetime.UTC)
        project = MagicMock()
        project.auto_delete_started_at = existing_start
        project.state = "active"
        project.auto_delete_minutes = 60

        self._call(project, {"auto_delete_minutes": 60})

        assert project.auto_delete_started_at == existing_start
        expected = existing_start + datetime.timedelta(minutes=60)
        assert project.lifetime_expires_at == expected
        assert project.auto_delete_warned is False


# ---------------------------------------------------------------------------
# _classify_single_disk
# ---------------------------------------------------------------------------


class TestClassifySingleDisk(unittest.TestCase):
    def _call(self, d, p_id, vm_node_id, dep_disk_libs, dep_disk_sizes, pool):
        from app.api.projects import _classify_single_disk

        return _classify_single_disk(
            d, p_id, vm_node_id, dep_disk_libs, dep_disk_sizes, pool
        )

    @patch("app.api.projects._resolve_disk_backing", return_value=(None, False))
    @patch(
        "app.api.projects._disk_path",
        return_value="/var/lib/troshka/vms/p1/vm1-d1.qcow2",
    )
    def test_new_disk(self, _mock_path, _mock_backing):
        d = {"node_id": "d1", "format": "qcow2", "bus": "virtio", "size_gb": 20}
        result = self._call(d, "p1", "vm1", {}, {}, None)

        assert result["is_new"] is True
        assert result["image_changed"] is False
        assert result["size_grew"] is False
        assert result["path"] == "/var/lib/troshka/vms/p1/vm1-d1.qcow2"
        assert result["format"] == "qcow2"
        assert result["bus"] == "virtio"
        assert result["size_gb"] == 20

    @patch("app.api.projects._resolve_disk_backing", return_value=(None, False))
    @patch("app.api.projects._disk_path", return_value="/path/d1.qcow2")
    def test_unchanged_disk(self, _mock_path, _mock_backing):
        d = {
            "node_id": "d1",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "library_item_id": "lib-old",
        }
        dep_libs = {"d1": "lib-old"}
        dep_sizes = {"d1": 20}
        result = self._call(d, "p1", "vm1", dep_libs, dep_sizes, None)

        assert result["is_new"] is False
        assert not result["image_changed"]
        assert result["size_grew"] is False

    @patch(
        "app.api.projects._resolve_disk_backing",
        return_value=("/cache/lib2.qcow2", True),
    )
    @patch("app.api.projects._disk_path", return_value="/path/d1.qcow2")
    def test_image_changed(self, _mock_path, _mock_backing):
        d = {
            "node_id": "d1",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "library_item_id": "lib-new",
        }
        dep_libs = {"d1": "lib-old"}
        dep_sizes = {"d1": 20}
        result = self._call(d, "p1", "vm1", dep_libs, dep_sizes, None)

        assert result["image_changed"]
        assert result["is_new"] is False
        assert result["is_library"] is True
        assert result["backing_file"] == "/cache/lib2.qcow2"

    @patch("app.api.projects._resolve_disk_backing", return_value=(None, False))
    @patch("app.api.projects._disk_path", return_value="/path/d1.qcow2")
    def test_size_grew(self, _mock_path, _mock_backing):
        d = {"node_id": "d1", "format": "qcow2", "bus": "virtio", "size_gb": 40}
        dep_libs = {"d1": None}
        dep_sizes = {"d1": 20}
        result = self._call(d, "p1", "vm1", dep_libs, dep_sizes, None)

        assert result["size_grew"] is True
        assert result["image_changed"] is False
        assert result["is_new"] is False


# ---------------------------------------------------------------------------
# _detect_disk_changes
# ---------------------------------------------------------------------------


class TestDetectDiskChanges(unittest.TestCase):
    def _call(self, p_id, vm_node_id, vm_disks, deployed, pool):
        from app.api.projects import _detect_disk_changes

        return _detect_disk_changes(p_id, vm_node_id, vm_disks, deployed, pool)

    @patch("app.api.projects._classify_single_disk")
    @patch("app.api.projects._get_deployed_disk_info", return_value=({}, {}))
    @patch(
        "app.services.deploy_topology._image_cache_path",
        return_value="/cache/iso1.iso",
    )
    def test_iso_goes_to_cdrom_list(self, _mock_cache, _mock_dep, _mock_classify):
        disks = [{"format": "iso", "library_item_id": "iso-1", "node_id": "d1"}]
        result = self._call("p1", "vm1", disks, {}, None)

        assert result["cdrom_list"] == ["/cache/iso1.iso"]
        assert result["disk_list"] == []
        _mock_classify.assert_not_called()

    @patch("app.api.projects._classify_single_disk")
    @patch("app.api.projects._get_deployed_disk_info", return_value=({}, {}))
    @patch("app.services.deploy_topology._image_cache_path")
    def test_iso_without_library_item_skipped(
        self, _mock_cache, _mock_dep, _mock_classify
    ):
        disks = [{"format": "iso", "node_id": "d1"}]
        result = self._call("p1", "vm1", disks, {}, None)

        assert result["cdrom_list"] == []
        _mock_cache.assert_not_called()

    @patch("app.api.projects._classify_single_disk")
    @patch("app.api.projects._get_deployed_disk_info", return_value=({}, {}))
    @patch("app.services.deploy_topology._image_cache_path")
    def test_new_disk_flagged(self, _mock_cache, _mock_dep, mock_classify):
        mock_classify.return_value = {
            "path": "/path/d1.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": None,
            "image_changed": False,
            "size_grew": False,
            "is_new": True,
            "is_library": False,
        }
        disks = [{"node_id": "d1", "format": "qcow2", "bus": "virtio", "size_gb": 20}]
        result = self._call("p1", "vm1", disks, {}, None)

        assert result["any_disk_changed"] is True
        assert len(result["disk_list"]) == 1
        assert len(result["disks_to_create"]) == 1

    @patch("app.api.projects._classify_single_disk")
    @patch("app.api.projects._get_deployed_disk_info", return_value=({}, {}))
    @patch("app.services.deploy_topology._image_cache_path")
    def test_image_change_adds_to_remove_list(
        self, _mock_cache, _mock_dep, mock_classify
    ):
        mock_classify.return_value = {
            "path": "/path/d1.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": "/cache/new.qcow2",
            "image_changed": True,
            "size_grew": False,
            "is_new": False,
            "is_library": True,
        }
        disks = [{"node_id": "d1", "format": "qcow2", "bus": "virtio", "size_gb": 20}]
        result = self._call("p1", "vm1", disks, {}, None)

        assert result["any_disk_changed"] is True
        assert "/path/d1.qcow2" in result["files_to_remove"]
        assert result["needs_library_download"] is True

    @patch("app.api.projects._classify_single_disk")
    @patch("app.api.projects._get_deployed_disk_info", return_value=({}, {}))
    @patch("app.services.deploy_topology._image_cache_path")
    def test_size_grew_without_image_change_adds_resize(
        self, _mock_cache, _mock_dep, mock_classify
    ):
        mock_classify.return_value = {
            "path": "/path/d1.qcow2",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 40,
            "backing_file": None,
            "image_changed": False,
            "size_grew": True,
            "is_new": False,
            "is_library": False,
        }
        disks = [{"node_id": "d1", "format": "qcow2", "bus": "virtio", "size_gb": 40}]
        result = self._call("p1", "vm1", disks, {}, None)

        assert result["any_disk_changed"] is True
        assert len(result["disks_to_resize"]) == 1
        assert result["disks_to_resize"][0]["new_size_gb"] == 40
        assert result["files_to_remove"] == []


# ---------------------------------------------------------------------------
# _enrich_project_response
# ---------------------------------------------------------------------------


class TestEnrichProjectResponse(unittest.TestCase):
    def _call(self, p, hosts_by_id, provs_by_id, owners_by_id):
        from app.api.projects import _enrich_project_response

        return _enrich_project_response(p, hosts_by_id, provs_by_id, owners_by_id)

    @patch("app.core.redis.get_job_info", return_value=None)
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    @patch("app.api.projects.ProjectResponse.model_validate")
    def test_basic_enrichment(self, mock_validate, _mock_prog, _mock_job):
        resp = MagicMock()
        mock_validate.return_value = resp

        project = MagicMock()
        project.owner_id = "user-1"
        project.host_id = "host-1"
        project.id = "proj-1"
        project.state = "active"

        owner = MagicMock()
        owner.email = "admin@example.com"

        host = MagicMock()
        host.instance_id = "i-abc123"
        host.ip_address = "10.0.0.1"
        host.provider_id = "prov-1"

        prov = MagicMock()
        prov.name = "AWS East"
        prov.type = "ec2"

        result = self._call(
            project, {"host-1": host}, {"prov-1": prov}, {"user-1": owner}
        )

        assert result is resp
        assert resp.owner_email == "admin@example.com"
        assert resp.host_instance_id == "i-abc123"
        assert resp.host_ip == "10.0.0.1"
        assert resp.host_provider_name == "AWS East"
        assert resp.host_provider_type == "ec2"

    @patch("app.core.redis.get_job_info", return_value=None)
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    @patch("app.api.projects.ProjectResponse.model_validate")
    def test_no_host(self, mock_validate, _mock_prog, _mock_job):
        resp = MagicMock()
        mock_validate.return_value = resp

        project = MagicMock()
        project.owner_id = "user-1"
        project.host_id = None
        project.id = "proj-1"
        project.state = "draft"

        result = self._call(project, {}, {}, {})
        assert result is resp

    @patch("app.core.redis.get_job_info", return_value=None)
    @patch("app.services.deploy_service._get_deploy_progress_data")
    @patch("app.api.projects.ProjectResponse.model_validate")
    def test_deploy_progress_attached(self, mock_validate, mock_prog, _mock_job):
        resp = MagicMock()
        mock_validate.return_value = resp
        mock_prog.return_value = {"step": "creating_vms", "detail": "bastion"}

        project = MagicMock()
        project.owner_id = "user-1"
        project.host_id = None
        project.id = "proj-1"
        project.state = "deploying"

        result = self._call(project, {}, {}, {})
        assert result.deploy_progress == {"step": "creating_vms", "detail": "bastion"}

    @patch("app.core.redis.get_job_info")
    @patch("app.services.deploy_service._get_deploy_progress_data", return_value=None)
    @patch("app.api.projects.ProjectResponse.model_validate")
    def test_queued_progress(self, mock_validate, _mock_prog, mock_job):
        resp = MagicMock()
        mock_validate.return_value = resp
        mock_job.return_value = {
            "status": "queued",
            "queue_position": 3,
            "queue_length": 5,
        }

        project = MagicMock()
        project.owner_id = "user-1"
        project.host_id = None
        project.id = "proj-1"
        project.state = "deploying"

        result = self._call(project, {}, {}, {})
        dp = result.deploy_progress
        assert dp["step"] == "queued"
        assert dp["queue_position"] == 3
        assert dp["queue_length"] == 5


# ---------------------------------------------------------------------------
# _cleanup_old_vm_files
# ---------------------------------------------------------------------------


class TestCleanupOldVmFiles(unittest.TestCase):
    def _call(self, h, p_id, target_vm_id, topology):
        from app.api.projects import _cleanup_old_vm_files

        return _cleanup_old_vm_files(h, p_id, target_vm_id, topology)

    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-42")
    @patch(
        "app.api.projects._seed_path",
        return_value="/var/lib/troshka/vms/p1/vm1/seed.iso",
    )
    @patch(
        "app.api.projects._disk_path",
        return_value="/var/lib/troshka/vms/p1/vm1/d1.qcow2",
    )
    @patch("app.api.projects._find_vm_disks")
    def test_successful_cleanup(
        self, mock_find, mock_dpath, mock_spath, mock_start, mock_wait
    ):
        mock_find.return_value = [
            {"node_id": "d1", "format": "qcow2"},
            {"node_id": "d2", "format": "iso"},  # ISOs are skipped
        ]

        h = MagicMock()
        self._call(h, "p1", "vm1", {"nodes": []})

        mock_start.assert_called_once()
        paths = mock_start.call_args.args[2]["paths"]
        assert "/var/lib/troshka/vms/p1/vm1/d1.qcow2" in paths
        assert "/var/lib/troshka/vms/p1/vm1/seed.iso" in paths
        mock_wait.assert_called_once_with(h, "job-42", timeout=15)

    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job")
    @patch("app.api.projects._seed_path", return_value="/seed.iso")
    @patch("app.api.projects._disk_path", return_value="/disk.qcow2")
    @patch(
        "app.api.projects._find_vm_disks",
        return_value=[{"node_id": "d1", "format": "qcow2"}],
    )
    def test_troshkad_error_caught(
        self, _mock_find, _mock_dpath, _mock_spath, mock_start, _mock_wait
    ):
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("connection refused")

        h = MagicMock()
        # Should NOT raise; error is logged as a warning
        self._call(h, "p1", "vm1", {"nodes": []})


# ---------------------------------------------------------------------------
# _wait_kubevirt_vms_ready
# ---------------------------------------------------------------------------


class TestWaitKubevirtVmsReady(unittest.TestCase):
    def _call(
        self,
        custom_api,
        ns,
        p_id,
        proj,
        s,
        deadline_secs=300,
        changed_cr_names=None,
    ):
        from app.api.projects import _wait_kubevirt_vms_ready

        return _wait_kubevirt_vms_ready(
            custom_api,
            ns,
            p_id,
            proj,
            s,
            changed_cr_names=changed_cr_names,
            deadline_secs=deadline_secs,
        )

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("time.sleep")
    @patch("time.time", side_effect=[1000, 1000])
    def test_all_ready(self, _mt, _ms, _mock_set, _mock_del):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"status": {"state": "Running"}, "spec": {"name": "vm-1"}},
                {"status": {"state": "Running"}, "spec": {"name": "vm-2"}},
            ]
        }

        result = self._call(custom_api, "ns-1", "p1", MagicMock(), MagicMock())
        assert result is None

    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("time.sleep")
    @patch("time.time", side_effect=[1000, 1000, 1000, 1000, 1005, 1005])
    def test_changed_vm_stale_generation_waits_then_settles(
        self, _mt, _ms, _mock_set, _mock_del, _notify
    ):
        # observedGeneration lags the spec generation until the operator finishes
        # reconciling; the waiter must keep polling until it catches up.
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = [
            {
                "items": [
                    {
                        "metadata": {"name": "vm-281550eb", "generation": 3},
                        "status": {"state": "Running", "observedGeneration": 2},
                        "spec": {"name": "rtr3"},
                    }
                ]
            },
            {
                "items": [
                    {
                        "metadata": {"name": "vm-281550eb", "generation": 3},
                        "status": {"state": "Reconfiguring", "observedGeneration": 2},
                        "spec": {"name": "rtr3"},
                    }
                ]
            },
            {
                "items": [
                    {
                        "metadata": {"name": "vm-281550eb", "generation": 3},
                        "status": {"state": "Running", "observedGeneration": 3},
                        "spec": {"name": "rtr3"},
                    }
                ]
            },
        ]
        result = self._call(
            custom_api,
            "ns-1",
            "p1",
            MagicMock(),
            MagicMock(),
            changed_cr_names=["vm-281550eb"],
            deadline_secs=30,
        )
        assert result is None
        assert custom_api.list_namespaced_custom_object.call_count == 3

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("time.sleep")
    @patch("time.time", side_effect=[1000, 1000])
    def test_changed_vm_observed_generation_ready_immediately(
        self, _mt, _ms, _mock_set, _mock_del
    ):
        # observedGeneration already matches the spec generation and the VM is
        # Running: the operator finished (or the change was a no-op), so the
        # waiter must return at once instead of burning the full deadline.
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "vm-281550eb", "generation": 4},
                    "status": {"state": "Running", "observedGeneration": 4},
                    "spec": {"name": "rtr3"},
                }
            ]
        }
        result = self._call(
            custom_api,
            "ns-1",
            "p1",
            MagicMock(),
            MagicMock(),
            changed_cr_names=["vm-281550eb"],
            deadline_secs=300,
        )
        assert result is None
        assert custom_api.list_namespaced_custom_object.call_count == 1

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("time.sleep")
    @patch("time.time", side_effect=[1000, 1000])
    def test_vm_error_returns_error(self, _mt, _ms, _mock_set, mock_del):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "status": {"state": "Error", "message": "disk full"},
                    "spec": {"name": "vm-1"},
                }
            ]
        }

        proj = MagicMock()
        s = MagicMock()
        result = self._call(custom_api, "ns-1", "p1", proj, s)

        assert result == "vm_error"
        assert proj.state == "error"
        assert "disk full" in proj.deploy_error
        s.commit.assert_called_once()
        mock_del.assert_called_once_with("p1")

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("time.sleep")
    @patch("time.time", side_effect=[1000, 1001, 1010])
    def test_timeout_returns_none(self, _mt, _ms, _mock_set, _mock_del):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"status": {"state": "Creating"}, "spec": {"name": "vm-1"}}]
        }

        result = self._call(
            custom_api, "ns-1", "p1", MagicMock(), MagicMock(), deadline_secs=5
        )
        assert result is None

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("time.sleep")
    @patch("time.time", side_effect=[1000, 1000])
    def test_api_exception_retries(self, _mt, _ms, _mock_set, _mock_del):
        """When the K8s API raises, all_ready stays False and the loop continues."""
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("timeout")

        # deadline_secs=0 effectively so only 1 iteration possible
        result = self._call(
            custom_api, "ns-1", "p1", MagicMock(), MagicMock(), deadline_secs=0
        )
        assert result is None


# ---------------------------------------------------------------------------
# _reconfigure_existing_vm
# ---------------------------------------------------------------------------


class TestReconfigureExistingVm(unittest.TestCase):
    def _call(
        self,
        h,
        p_id,
        s,
        current,
        deployed,
        vm,
        vni_map,
        restart_ids,
        pool,
        diff,
        errors,
    ):
        from app.api.projects import _reconfigure_existing_vm

        return _reconfigure_existing_vm(
            h, p_id, s, current, deployed, vm, vni_map, restart_ids, pool, diff, errors
        )

    @patch("app.services.deploy_service._set_deploy_progress")
    @patch(
        "app.services.deploy_topology._vm_domain_name",
        return_value="troshka-p1-vm1",
    )
    @patch("app.services.deploy_topology._resolve_boot_devs", return_value=["hd"])
    @patch("app.api.projects._apply_disk_changes")
    @patch("app.api.projects.troshkad_reconfigure_vm")
    @patch("app.api.projects.troshkad_get_vm_config")
    @patch("app.api.projects._detect_disk_changes")
    @patch("app.api.projects._find_vm_networks")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.api.projects._seed_path", return_value="/seed.iso")
    def test_vm_not_on_host_added_to_diff(
        self,
        _mock_seed,
        _mock_fvd,
        _mock_fvn,
        mock_detect,
        mock_config,
        _mock_reconf,
        _mock_apply,
        _mock_boot,
        _mock_dom,
        _mock_prog,
    ):
        mock_detect.return_value = {
            "disk_list": [],
            "cdrom_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        }
        mock_config.return_value = None  # VM not found on host

        vm = {"node_id": "vm-1", "name": "test", "vcpus": 2, "ram_gb": 4}
        current = {"nodes": [{"id": "vm-1", "type": "vmNode", "data": {}}]}
        diff = {"added_vms": []}
        errors = []

        self._call(
            MagicMock(),
            "p1",
            MagicMock(),
            current,
            {},
            vm,
            {},
            set(),
            None,
            diff,
            errors,
        )

        assert len(diff["added_vms"]) == 1
        assert diff["added_vms"][0]["id"] == "vm-1"

    @patch("app.services.deploy_service._set_deploy_progress")
    @patch(
        "app.services.deploy_topology._vm_domain_name",
        return_value="troshka-p1-vm1",
    )
    @patch("app.services.deploy_topology._resolve_boot_devs", return_value=["hd"])
    @patch("app.api.projects._apply_disk_changes")
    @patch("app.api.projects.troshkad_reconfigure_vm")
    @patch("app.api.projects.troshkad_get_vm_config")
    @patch("app.api.projects._detect_disk_changes")
    @patch("app.api.projects._find_vm_networks")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.api.projects._seed_path", return_value="/seed.iso")
    def test_unchanged_vm_skips_reconfigure(
        self,
        _mock_seed,
        _mock_fvd,
        mock_fvn,
        mock_detect,
        mock_config,
        mock_reconf,
        _mock_apply,
        _mock_boot,
        _mock_dom,
        _mock_prog,
    ):
        mock_fvn.return_value = [{"bridge": "br-100", "mac": "52:54:00:11:22:33"}]
        mock_detect.return_value = {
            "disk_list": [
                {"path": "/path/d1.qcow2", "format": "qcow2", "bus": "virtio"}
            ],
            "cdrom_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        }
        mock_config.return_value = {
            "boot_devs": ["hd"],
            "vcpus": 2,
            "ram_mb": 4096,
            "nics": [{"bridge": "br-100"}],
            "disks": ["/path/d1.qcow2"],
            "cdroms": [],
        }

        vm = {"node_id": "vm-1", "name": "test", "vcpus": 2, "ram_gb": 4}
        diff = {"added_vms": []}
        errors = []

        self._call(
            MagicMock(), "p1", MagicMock(), {}, {}, vm, {}, set(), None, diff, errors
        )

        mock_reconf.assert_not_called()
        assert errors == []

    @patch("app.services.deploy_service._set_deploy_progress")
    @patch(
        "app.services.deploy_topology._vm_domain_name",
        return_value="troshka-p1-vm1",
    )
    @patch("app.services.deploy_topology._resolve_boot_devs", return_value=["hd"])
    @patch("app.api.projects._apply_disk_changes")
    @patch("app.api.projects.troshkad_reconfigure_vm")
    @patch("app.api.projects.troshkad_get_vm_config")
    @patch("app.api.projects._detect_disk_changes")
    @patch("app.api.projects._find_vm_networks")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.api.projects._seed_path", return_value="/seed.iso")
    def test_reconfigure_error_appended(
        self,
        _mock_seed,
        _mock_fvd,
        mock_fvn,
        mock_detect,
        mock_config,
        mock_reconf,
        _mock_apply,
        _mock_boot,
        _mock_dom,
        _mock_prog,
    ):
        from app.services.troshkad_client import TroshkadError

        mock_fvn.return_value = [{"bridge": "br-100", "mac": "52:54:00:11:22:33"}]
        mock_detect.return_value = {
            "disk_list": [
                {"path": "/path/d1.qcow2", "format": "qcow2", "bus": "virtio"}
            ],
            "cdrom_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        }
        # Return a config with different vcpus to trigger reconfigure
        mock_config.return_value = {
            "boot_devs": ["hd"],
            "vcpus": 4,  # different from vm["vcpus"]=2
            "ram_mb": 4096,
            "nics": [{"bridge": "br-100"}],
            "disks": ["/path/d1.qcow2"],
            "cdroms": [],
        }
        mock_reconf.side_effect = TroshkadError("connection refused")

        vm = {"node_id": "vm-1", "name": "test", "vcpus": 2, "ram_gb": 4}
        errors = []

        self._call(
            MagicMock(),
            "p1",
            MagicMock(),
            {},
            {},
            vm,
            {},
            set(),
            None,
            {"added_vms": []},
            errors,
        )

        assert len(errors) == 1
        assert "connection refused" in errors[0]


# ---------------------------------------------------------------------------
# _finalize_reconfigure
# ---------------------------------------------------------------------------


class TestFinalizeReconfigure(unittest.TestCase):
    def _call(self, s, proj, h, p_id, current, deployed, errors):
        from app.api.projects import _finalize_reconfigure

        return _finalize_reconfigure(s, proj, h, p_id, current, deployed, errors)

    @patch("app.api.projects._broadcast_vm_states")
    @patch("app.api.projects._reconfigure_bmc")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_topology._extract_bmc_config", return_value=None)
    @patch("app.services.placement.sync_host_capacity")
    def test_no_errors_sets_active_and_copies_topology(
        self, _mock_sync, _mock_bmc, _mock_del, mock_notify, _mock_rbmc, _mock_bcast
    ):
        s = MagicMock()
        proj = MagicMock()
        proj.topology = {"nodes": [{"id": "vm-1", "data": {"name": "test"}}]}
        h = MagicMock()
        p_id = "proj-123"

        self._call(s, proj, h, p_id, {"nodes": []}, {"nodes": []}, [])

        assert proj.state == "active"
        assert proj.deploy_error is None
        assert proj.deployed_topology is not None
        assert proj.deployed_topology["nodes"][0]["id"] == "vm-1"
        s.commit.assert_called_once()
        mock_notify.assert_called_once()

    @patch("app.api.projects._broadcast_vm_states")
    @patch("app.api.projects._reconfigure_bmc")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_topology._extract_bmc_config", return_value=None)
    @patch("app.services.placement.sync_host_capacity")
    def test_errors_joined_into_deploy_error(
        self, _mock_sync, _mock_bmc, _mock_del, _mock_notify, _mock_rbmc, _mock_bcast
    ):
        s = MagicMock()
        proj = MagicMock()
        proj.topology = {"nodes": []}
        h = MagicMock()

        errors = ["VM vm-1 failed: timeout", "VM vm-2 failed: disk"]
        self._call(s, proj, h, "proj-123", {}, {}, errors)

        assert proj.state == "active"
        assert "VM vm-1 failed: timeout" in proj.deploy_error
        assert "VM vm-2 failed: disk" in proj.deploy_error
        s.commit.assert_called_once()

    @patch("app.api.projects._broadcast_vm_states")
    @patch("app.api.projects._reconfigure_bmc")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_topology._extract_bmc_config")
    @patch("app.services.placement.sync_host_capacity")
    def test_bmc_config_added_to_deployed_topology(
        self,
        _mock_sync,
        mock_bmc_extract,
        _mock_del,
        _mock_notify,
        _mock_rbmc,
        _mock_bcast,
    ):
        mock_bmc_extract.return_value = {
            "bmc_network": {"bmcUsername": "root", "bmcPassword": "s3cret"},
            "vms": [
                {
                    "node_id": "vm-1",
                    "bmc_ip": "192.168.99.10",
                    "domain_name": "troshka-p1-vm1",
                }
            ],
        }

        s = MagicMock()
        proj = MagicMock()
        proj.topology = {"nodes": [{"id": "vm-1"}]}
        h = MagicMock()

        self._call(s, proj, h, "p1", {}, {}, [])

        dt = proj.deployed_topology
        assert "bmc" in dt
        assert dt["bmc"]["username"] == "root"
        assert dt["bmc"]["password"] == "s3cret"
        assert "vm-1" in dt["bmc"]["vms"]
        vm_bmc = dt["bmc"]["vms"]["vm-1"]
        assert "redfish" in vm_bmc["redfish_url"]
        assert "623" in vm_bmc["ipmi_address"]


if __name__ == "__main__":
    unittest.main()
