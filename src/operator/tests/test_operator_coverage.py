"""Tests covering uncovered lines in operator handlers/project.py and handlers/vm.py.

Each test class targets a specific function identified from coverage gaps.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch, call, AsyncMock
from kubernetes.client.exceptions import ApiException


class MockPatch:
    """Mock for kopf's patch object (dict-like .status)."""

    def __init__(self):
        self.status = {}


# ---------------------------------------------------------------------------
# handlers/project.py — _fetch_vmi_states
# ---------------------------------------------------------------------------


class TestFetchVmiStates:
    def test_returns_states_for_vmis(self):
        from handlers.project import _fetch_vmi_states

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "vm-a"},
                    "status": {"phase": "Running"},
                },
                {
                    "metadata": {"name": "vm-b"},
                    "status": {"phase": "Scheduling"},
                },
            ]
        }
        result = _fetch_vmi_states(custom_api, "test-ns")
        assert result == {"vm-a": "Running", "vm-b": "Scheduling"}
        custom_api.list_namespaced_custom_object.assert_called_once_with(
            group="kubevirt.io",
            version="v1",
            namespace="test-ns",
            plural="virtualmachineinstances",
        )

    def test_returns_empty_dict_on_exception(self):
        from handlers.project import _fetch_vmi_states

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("timeout")
        result = _fetch_vmi_states(custom_api, "test-ns")
        assert result == {}

    def test_handles_missing_status(self):
        from handlers.project import _fetch_vmi_states

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "vm-c"}},
            ]
        }
        result = _fetch_vmi_states(custom_api, "test-ns")
        assert result == {"vm-c": ""}


# ---------------------------------------------------------------------------
# handlers/project.py — _patch_vm_states
# ---------------------------------------------------------------------------


class TestPatchVmStates:
    def test_patches_when_states_differ(self):
        from handlers.project import _patch_vm_states

        p = MockPatch()
        status = {"vmStates": {"vm-a": "Running"}}
        _patch_vm_states(status, p, {"vm-a": "Stopped"}, {})
        assert p.status["vmStates"] == {"vm-a": "Stopped"}

    def test_no_patch_when_states_equal(self):
        from handlers.project import _patch_vm_states

        p = MockPatch()
        status = {"vmStates": {"vm-a": "Running"}}
        _patch_vm_states(status, p, {"vm-a": "Running"}, {})
        assert "vmStates" not in p.status

    def test_patches_scheduling_errors(self):
        from handlers.project import _patch_vm_states

        p = MockPatch()
        status = {}
        errors = {"vm-x": "Insufficient memory"}
        _patch_vm_states(status, p, {}, errors)
        assert p.status["schedulingErrors"] == errors

    def test_no_patch_scheduling_errors_when_equal(self):
        from handlers.project import _patch_vm_states

        p = MockPatch()
        status = {"schedulingErrors": {"vm-x": "Insufficient memory"}}
        _patch_vm_states(status, p, {}, {"vm-x": "Insufficient memory"})
        assert "schedulingErrors" not in p.status


# ---------------------------------------------------------------------------
# handlers/project.py — _collect_bmc_vms
# ---------------------------------------------------------------------------


class TestCollectBmcVms:
    def test_filters_bmc_enabled_vms(self):
        from handlers.project import _collect_bmc_vms

        vm_items = [
            {
                "spec": {
                    "bmcEnabled": True,
                    "vmId": "vm-1",
                    "smbiosUuid": "uuid-1",
                    "bmcIp": "10.0.0.5",
                },
                "status": {"domainUuid": "dom-1"},
            },
            {
                "spec": {"bmcEnabled": False, "vmId": "vm-2"},
                "status": {},
            },
        ]
        result = _collect_bmc_vms(vm_items)
        assert len(result) == 1
        assert result[0]["vmId"] == "vm-1"
        assert result[0]["smbiosUuid"] == "uuid-1"
        assert result[0]["bmcIp"] == "10.0.0.5"
        assert result[0]["domainUuid"] == "dom-1"

    def test_empty_when_no_bmc(self):
        from handlers.project import _collect_bmc_vms

        vm_items = [
            {"spec": {"bmcEnabled": False}, "status": {}},
        ]
        assert _collect_bmc_vms(vm_items) == []


# ---------------------------------------------------------------------------
# handlers/project.py — _enrich_bmc_ips
# ---------------------------------------------------------------------------


class TestEnrichBmcIps:
    @patch("handlers.project._get_bmc_ips_from_topology")
    def test_fills_missing_ips(self, mock_get_ips):
        from handlers.project import _enrich_bmc_ips

        mock_get_ips.return_value = {"vm-1": "10.0.0.10"}
        bmc_vms = [{"vmId": "vm-1", "bmcIp": ""}]
        custom_api = MagicMock()
        _enrich_bmc_ips(bmc_vms, custom_api, "test-ns")
        assert bmc_vms[0]["bmcIp"] == "10.0.0.10"

    @patch("handlers.project._get_bmc_ips_from_topology")
    def test_skips_when_all_ips_present(self, mock_get_ips):
        from handlers.project import _enrich_bmc_ips

        bmc_vms = [{"vmId": "vm-1", "bmcIp": "10.0.0.5"}]
        custom_api = MagicMock()
        _enrich_bmc_ips(bmc_vms, custom_api, "test-ns")
        mock_get_ips.assert_not_called()


# ---------------------------------------------------------------------------
# handlers/project.py — _get_bmc_credentials
# ---------------------------------------------------------------------------


class TestGetBmcCredentials:
    def test_extracts_credentials_from_topology(self):
        from handlers.project import _get_bmc_credentials

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "spec": {
                        "topology": {
                            "nodes": [
                                {
                                    "data": {
                                        "networkType": "bmc",
                                        "bmcUsername": "admin",
                                        "bmcPassword": "test-bmc-pass",  # pragma: allowlist secret,
                                    }
                                }
                            ]
                        }
                    }
                }
            ]
        }
        result = _get_bmc_credentials(custom_api, "test-ns")
        assert result == {
            "username": "admin",
            "password": "test-bmc-pass",  # pragma: allowlist secret
        }

    def test_returns_empty_on_no_bmc_node(self):
        from handlers.project import _get_bmc_credentials

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "spec": {
                        "topology": {"nodes": [{"data": {"networkType": "internal"}}]}
                    }
                }
            ]
        }
        result = _get_bmc_credentials(custom_api, "test-ns")
        assert result == {}

    def test_returns_empty_on_exception(self):
        from handlers.project import _get_bmc_credentials

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("fail")
        result = _get_bmc_credentials(custom_api, "test-ns")
        assert result == {}


# ---------------------------------------------------------------------------
# handlers/project.py — _get_bmc_ips_from_topology
# ---------------------------------------------------------------------------


class TestGetBmcIpsFromTopology:
    def test_extracts_ips_from_vm_nodes(self):
        from handlers.project import _get_bmc_ips_from_topology

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "spec": {
                        "topology": {
                            "nodes": [
                                {
                                    "type": "vmNode",
                                    "id": "node-1",
                                    "data": {
                                        "id": "vm-1",
                                        "bmcIp": "10.0.0.5",
                                    },
                                },
                                {
                                    "type": "networkNode",
                                    "id": "net-1",
                                    "data": {"bmcIp": "10.0.0.6"},
                                },
                            ]
                        }
                    }
                }
            ]
        }
        result = _get_bmc_ips_from_topology(custom_api, "test-ns")
        assert result == {"vm-1": "10.0.0.5"}

    def test_returns_empty_on_no_bmc_ips(self):
        from handlers.project import _get_bmc_ips_from_topology

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "spec": {
                        "topology": {
                            "nodes": [
                                {"type": "vmNode", "id": "n1", "data": {"id": "v1"}}
                            ]
                        }
                    }
                }
            ]
        }
        result = _get_bmc_ips_from_topology(custom_api, "test-ns")
        assert result == {}

    def test_returns_empty_on_exception(self):
        from handlers.project import _get_bmc_ips_from_topology

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("fail")
        result = _get_bmc_ips_from_topology(custom_api, "test-ns")
        assert result == {}


# ---------------------------------------------------------------------------
# handlers/project.py — _handle_recert
# ---------------------------------------------------------------------------


class TestHandleRecert:
    @patch("handlers.project.client")
    def test_returns_false_when_no_recert_config(self, mock_client):
        from handlers.project import _handle_recert

        p = MockPatch()
        result = _handle_recert({}, "ns", "proj", p)
        assert result is False

    @patch("handlers.project.client")
    def test_returns_false_when_recert_done(self, mock_client):
        from handlers.project import _handle_recert

        p = MockPatch()
        status = {"recertConfig": [{"vmName": "vm-a"}], "recertDone": True}
        result = _handle_recert(status, "ns", "proj", p)
        assert result is False

    @patch("handlers.project._check_recert_pvcs_ready", return_value=False)
    @patch("handlers.project.client")
    def test_returns_true_when_pvcs_not_ready(self, mock_client, mock_check):
        from handlers.project import _handle_recert

        p = MockPatch()
        status = {"recertConfig": [{"vmName": "vm-a"}]}
        result = _handle_recert(status, "ns", "proj", p)
        assert result is True
        assert p.status["deployProgress"]["stage"] == "Preparing disks"

    @patch("handlers.project._check_recert_pvcs_ready", return_value=True)
    @patch("handlers.project._create_recert_jobs", return_value="Job failed")
    @patch("handlers.project.client")
    def test_sets_error_when_recert_jobs_fail(
        self, mock_client, mock_create, mock_check
    ):
        from handlers.project import _handle_recert

        p = MockPatch()
        status = {"recertConfig": [{"vmName": "vm-a"}]}
        result = _handle_recert(status, "ns", "proj", p)
        assert result is True
        assert p.status["phase"] == "Error"
        assert p.status["error"] == "Job failed"

    @patch("handlers.project._finalize_recert")
    @patch(
        "handlers.project._poll_recert_jobs",
        return_value=(True, False),
    )
    @patch("handlers.project._create_recert_jobs", return_value=None)
    @patch("handlers.project._check_recert_pvcs_ready", return_value=True)
    @patch("handlers.project.client")
    def test_finalizes_when_all_done(
        self, mock_client, mock_check, mock_create, mock_poll, mock_finalize
    ):
        from handlers.project import _handle_recert

        p = MockPatch()
        status = {"recertConfig": [{"vmName": "vm-a"}]}
        result = _handle_recert(status, "ns", "proj", p)
        assert result is False
        assert p.status["recertDone"] is True
        mock_finalize.assert_called_once()


# ---------------------------------------------------------------------------
# handlers/project.py — _cleanup_stale_volumes
# ---------------------------------------------------------------------------


class TestCleanupStaleVolumes:
    @patch("handlers.project._find_stale_volume_attachments", return_value=["va-1"])
    @patch("handlers.project.client")
    def test_deletes_stale_and_returns_true(self, mock_client, mock_find):
        from handlers.project import _cleanup_stale_volumes

        p = MockPatch()
        storage_api = mock_client.StorageV1Api.return_value
        result = _cleanup_stale_volumes("ns", "proj", p)
        assert result is True
        storage_api.delete_volume_attachment.assert_called_once_with(name="va-1")
        assert p.status["deployProgress"]["stage"] == "Releasing disks"

    @patch("handlers.project._find_stale_volume_attachments", return_value=[])
    @patch("handlers.project.client")
    def test_returns_false_when_no_stale(self, mock_client, mock_find):
        from handlers.project import _cleanup_stale_volumes

        p = MockPatch()
        result = _cleanup_stale_volumes("ns", "proj", p)
        assert result is False

    @patch("handlers.project._find_stale_volume_attachments")
    @patch("handlers.project.client")
    def test_returns_false_on_exception(self, mock_client, mock_find):
        from handlers.project import _cleanup_stale_volumes

        mock_find.side_effect = Exception("API error")
        p = MockPatch()
        result = _cleanup_stale_volumes("ns", "proj", p)
        assert result is False


# ---------------------------------------------------------------------------
# handlers/project.py — _handle_vm_start
# ---------------------------------------------------------------------------


class TestHandleVmStart:
    @patch("handlers.project._start_kubevirt_vms")
    @patch("handlers.project._cleanup_stale_volumes", return_value=False)
    def test_marks_started_when_all_vms_start(self, mock_cleanup, mock_start):
        from handlers.project import _handle_vm_start

        vm_items = [{"metadata": {"name": "vm-1"}}, {"metadata": {"name": "vm-2"}}]
        mock_start.return_value = 2
        p = MockPatch()
        custom_api = MagicMock()
        result = _handle_vm_start({}, "ns", "proj", p, custom_api, vm_items)
        assert result is False
        assert p.status["vmsStarted"] is True

    @patch("handlers.project._start_kubevirt_vms")
    @patch("handlers.project._cleanup_stale_volumes", return_value=False)
    def test_returns_true_when_partial_start(self, mock_cleanup, mock_start):
        from handlers.project import _handle_vm_start

        vm_items = [{"metadata": {"name": "vm-1"}}, {"metadata": {"name": "vm-2"}}]
        mock_start.return_value = 1
        p = MockPatch()
        custom_api = MagicMock()
        result = _handle_vm_start({}, "ns", "proj", p, custom_api, vm_items)
        assert result is True
        assert p.status["deployProgress"]["stage"] == "Starting VMs"

    def test_returns_true_when_recert_not_cleaned(self):
        from handlers.project import _handle_vm_start

        p = MockPatch()
        status = {"recertConfig": [{"vmName": "a"}]}
        result = _handle_vm_start(status, "ns", "proj", p, MagicMock(), [])
        assert result is True

    def test_returns_false_when_already_started(self):
        from handlers.project import _handle_vm_start

        p = MockPatch()
        result = _handle_vm_start(
            {"vmsStarted": True}, "ns", "proj", p, MagicMock(), []
        )
        assert result is False


# ---------------------------------------------------------------------------
# handlers/project.py — _handle_deploying_phase
# ---------------------------------------------------------------------------


class TestHandleDeployingPhase:
    @patch("handlers.project._handle_vm_start", return_value=False)
    @patch("handlers.project._handle_recert", return_value=False)
    def test_marks_running_when_all_ready(self, mock_recert, mock_start):
        from handlers.project import _handle_deploying_phase

        vm_items = [{"spec": {}}, {"spec": {}}]
        p = MockPatch()
        _handle_deploying_phase({}, "ns", "proj", p, MagicMock(), vm_items, 2)
        assert p.status["phase"] == "Running"
        assert p.status["deployProgress"]["percent"] == 100

    @patch("handlers.project._handle_vm_start", return_value=False)
    @patch("handlers.project._handle_recert", return_value=False)
    def test_progress_when_not_all_ready(self, mock_recert, mock_start):
        from handlers.project import _handle_deploying_phase

        vm_items = [{"spec": {}}, {"spec": {}}]
        p = MockPatch()
        _handle_deploying_phase({}, "ns", "proj", p, MagicMock(), vm_items, 1)
        assert p.status["deployProgress"]["percent"] == 95
        assert "phase" not in p.status

    @patch("handlers.project._handle_recert", return_value=True)
    def test_returns_early_when_recert_blocks(self, mock_recert):
        from handlers.project import _handle_deploying_phase

        p = MockPatch()
        _handle_deploying_phase({}, "ns", "proj", p, MagicMock(), [], 0)
        assert "phase" not in p.status

    @patch("handlers.project._handle_vm_start", return_value=False)
    @patch("handlers.project._handle_recert", return_value=False)
    def test_sets_recert_cleaned(self, mock_recert, mock_start):
        from handlers.project import _handle_deploying_phase

        p = MockPatch()
        status = {"recertDone": True}
        _handle_deploying_phase(status, "ns", "proj", p, MagicMock(), [], 0)
        assert p.status["recertCleaned"] is True


# ---------------------------------------------------------------------------
# handlers/project.py — _ensure_bmc_deployment
# ---------------------------------------------------------------------------


class TestEnsureBmcDeployment:
    @patch("handlers.project._collect_bmc_vms", return_value=[])
    def test_returns_early_when_no_bmc_vms(self, mock_collect):
        from handlers.project import _ensure_bmc_deployment

        _ensure_bmc_deployment([], "ns")

    @patch("handlers.project.client")
    @patch("handlers.project._collect_bmc_vms")
    def test_returns_early_when_deployment_exists(self, mock_collect, mock_client):
        from handlers.project import _ensure_bmc_deployment

        mock_collect.return_value = [{"vmId": "vm-1", "bmcIp": "10.0.0.5"}]
        apps_api = mock_client.AppsV1Api.return_value
        apps_api.read_namespaced_deployment.return_value = MagicMock()
        _ensure_bmc_deployment([], "troshka-proj123")
        apps_api.create_namespaced_deployment.assert_not_called()

    @patch("helpers.bmc.build_bmc_deployment", return_value={"metadata": {}})
    @patch("handlers.vm._find_bmc_nad", return_value="bmc-nad-1")
    @patch("handlers.vm._ensure_bmc_sa_and_rbac")
    @patch("handlers.project._enrich_bmc_ips")
    @patch("handlers.project._get_bmc_credentials", return_value={"username": "admin"})
    @patch("handlers.project.client")
    @patch("handlers.project._collect_bmc_vms")
    def test_recreates_when_404(
        self,
        mock_collect,
        mock_client,
        mock_creds,
        mock_enrich,
        mock_sa,
        mock_find_nad,
        mock_build,
    ):
        from handlers.project import _ensure_bmc_deployment

        mock_collect.return_value = [{"vmId": "vm-1", "bmcIp": "10.0.0.5"}]
        apps_api = mock_client.AppsV1Api.return_value
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=404)
        _ensure_bmc_deployment([], "troshka-proj123")
        apps_api.create_namespaced_deployment.assert_called_once()

    @patch("handlers.project.client")
    @patch("handlers.project._collect_bmc_vms")
    def test_returns_early_on_non_404_error(self, mock_collect, mock_client):
        from handlers.project import _ensure_bmc_deployment

        mock_collect.return_value = [{"vmId": "vm-1", "bmcIp": "10.0.0.5"}]
        apps_api = mock_client.AppsV1Api.return_value
        apps_api.read_namespaced_deployment.side_effect = ApiException(status=500)
        _ensure_bmc_deployment([], "troshka-proj123")
        apps_api.create_namespaced_deployment.assert_not_called()


# ---------------------------------------------------------------------------
# handlers/project.py — project_delete
# ---------------------------------------------------------------------------


class TestProjectDelete:
    @staticmethod
    def _get_project_delete_fn():
        """Extract the unwrapped project_delete async function from kopf mock."""
        import importlib
        import kopf

        importlib.import_module("handlers.project")
        decorator_mock = kopf.on.delete.return_value
        for call_args in reversed(decorator_mock.call_args_list):
            fn = call_args[0][0]
            if asyncio.iscoroutinefunction(fn) and fn.__name__ == "project_delete":
                return fn
        raise RuntimeError("Could not find project_delete in kopf mock call args")

    @patch("handlers.project._remove_sa_from_sccs")
    @patch("handlers.project._delete_custom_resources")
    @patch("handlers.project.client")
    def test_deletes_all_resource_types(self, mock_client, mock_del, mock_scc):
        project_delete = self._get_project_delete_fn()

        asyncio.run(project_delete(namespace="troshka-abc", name="abc"))
        # Should call _delete_custom_resources 5 times (VMIs, VMs, DVs, NADs, Routes)
        assert mock_del.call_count == 5
        # Should call _remove_sa_from_sccs 3 times
        assert mock_scc.call_count == 3


# ---------------------------------------------------------------------------
# handlers/project.py — _handle_capture
# ---------------------------------------------------------------------------


class TestHandleCapture:
    @patch("handlers.project._cleanup_capture_resources")
    @patch("handlers.project._read_export_sizes")
    @patch("handlers.project._poll_export_jobs", return_value=None)
    @patch("handlers.project._snapshot_and_export_disk")
    @patch("handlers.project._stop_all_vms")
    @patch("handlers.project.client")
    def test_capture_happy_path(
        self,
        mock_client,
        mock_stop,
        mock_snap,
        mock_poll,
        mock_sizes,
        mock_cleanup,
    ):
        from handlers.project import _handle_capture

        mock_snap.return_value = {
            "diskId": "d1",
            "vmId": "v1",
            "s3Key": "key1",
            "format": "qcow2",
            "virtualSizeBytes": 1024,
            "jobName": "job-1",
        }
        p = MockPatch()
        capture_config = {
            "s3Config": {"bucket": "test"},
            "disks": [{"diskId": "d1", "vmName": "vm1"}],
        }
        asyncio.run(_handle_capture(capture_config, "ns", "proj", p))
        assert p.status["phase"] == "CaptureComplete"
        assert p.status["captureProgress"] == "Done"
        assert len(p.status["capturedDisks"]) == 1

    @patch("handlers.project._cleanup_capture_resources")
    @patch("handlers.project._poll_export_jobs", return_value="export failed")
    @patch("handlers.project._snapshot_and_export_disk")
    @patch("handlers.project._stop_all_vms")
    @patch("handlers.project.client")
    def test_capture_returns_early_on_poll_error(
        self, mock_client, mock_stop, mock_snap, mock_poll, mock_cleanup
    ):
        from handlers.project import _handle_capture

        mock_snap.return_value = {"diskId": "d1", "jobName": "j1"}
        p = MockPatch()
        capture_config = {"s3Config": {}, "disks": [{"diskId": "d1", "vmName": "vm1"}]}
        asyncio.run(_handle_capture(capture_config, "ns", "proj", p))
        assert p.status.get("phase") != "CaptureComplete"
        mock_cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# handlers/vm.py — _clone_s3_disk
# ---------------------------------------------------------------------------


class TestCloneS3Disk:
    @patch("handlers.vm._wait_for_datavolume", return_value=True)
    @patch("handlers.vm.build_clone_datavolume", return_value={"metadata": {}})
    @patch("handlers.vm._ensure_golden_pvc", return_value="golden-abc")
    @patch(
        "handlers.vm._resolve_disk_s3", return_value=("s3://path", {}, "test-key")
    )  # pragma: allowlist secret
    @patch("handlers.vm.owner_ref", return_value={})
    @patch("handlers.vm.client")
    def test_clone_success(
        self,
        mock_client,
        mock_oref,
        mock_resolve,
        mock_golden,
        mock_build_dv,
        mock_wait,
    ):
        from handlers.vm import _clone_s3_disk

        p = MockPatch()
        body = {"kind": "TroshkaVM", "metadata": {"name": "vm-1", "uid": "u1"}}
        result = asyncio.run(
            _clone_s3_disk(
                "disk-1",
                "pvc-1",
                {"sizeGb": 20},
                "vm-1",
                "ns",
                body,
                MagicMock(),
                MagicMock(),
                {},
                {},
                p,
            )
        )
        assert result is True

    @patch("handlers.vm._resolve_disk_s3", return_value=(None, None, None))
    @patch("handlers.vm.client")
    def test_clone_returns_false_when_no_s3_path(self, mock_client, mock_resolve):
        from handlers.vm import _clone_s3_disk

        p = MockPatch()
        body = {"kind": "TroshkaVM", "metadata": {"name": "vm-1", "uid": "u1"}}
        result = asyncio.run(
            _clone_s3_disk(
                "disk-1",
                "pvc-1",
                {},
                "vm-1",
                "ns",
                body,
                MagicMock(),
                MagicMock(),
                {},
                {},
                p,
            )
        )
        assert result is False


# ---------------------------------------------------------------------------
# handlers/vm.py — _provision_new_disks
# ---------------------------------------------------------------------------


class TestProvisionNewDisks:
    @patch("handlers.vm._clone_s3_disk", return_value=False)
    @patch("handlers.vm.build_blank_pvc", return_value={"metadata": {}})
    @patch("handlers.vm.owner_ref", return_value={})
    @patch("handlers.vm.client")
    def test_creates_blank_pvc_when_not_cloned(
        self, mock_client, mock_oref, mock_blank, mock_clone
    ):
        from handlers.vm import _provision_new_disks

        p = MockPatch()
        body = {"kind": "TroshkaVM", "metadata": {"name": "vm-1", "uid": "u1"}}
        core_api = MagicMock()
        disk_id = "d1aaaaaa-0000-0000-0000-000000000000"
        new_disks = {disk_id: {"id": disk_id, "blank": True, "sizeGb": 10}}
        result = asyncio.run(
            _provision_new_disks(
                new_disks, {}, "vm-1", "ns", body, core_api, MagicMock(), {}, {}, p
            )
        )
        assert result == {disk_id: f"vm-1-disk-{disk_id[:8]}"}
        core_api.create_namespaced_persistent_volume_claim.assert_called_once()

    @patch("handlers.vm._clone_s3_disk", return_value=True)
    @patch("handlers.vm.client")
    def test_skips_existing_disks(self, mock_client, mock_clone):
        from handlers.vm import _provision_new_disks

        p = MockPatch()
        body = {"kind": "TroshkaVM", "metadata": {"name": "vm-1", "uid": "u1"}}
        disk_id = "d1aaaaaa-0000-0000-0000-000000000000"
        new_disks = {disk_id: {"id": disk_id}}
        old_disks = {disk_id: {"id": disk_id}}
        result = asyncio.run(
            _provision_new_disks(
                new_disks,
                old_disks,
                "vm-1",
                "ns",
                body,
                MagicMock(),
                MagicMock(),
                {},
                {},
                p,
            )
        )
        assert result == {disk_id: f"vm-1-disk-{disk_id[:8]}"}
        mock_clone.assert_not_called()


# ---------------------------------------------------------------------------
# handlers/vm.py — _reconcile_disks
# ---------------------------------------------------------------------------


class TestReconcileDisks:
    @patch("handlers.vm._delete_removed_disks")
    @patch("handlers.vm._provision_new_disks", return_value={"d1": "pvc-1"})
    @patch("handlers.vm._get_central_s3_config_from_project", return_value={})
    @patch("handlers.vm._get_s3_config_from_project", return_value={})
    @patch("handlers.vm.client")
    def test_reconcile_calls_provision_and_delete(
        self, mock_client, mock_s3, mock_central, mock_provision, mock_delete
    ):
        from handlers.vm import _reconcile_disks

        p = MockPatch()
        body = {"kind": "TroshkaVM", "metadata": {"name": "vm-1", "uid": "u1"}}
        old_spec = {"disks": [{"id": "d0"}]}
        new_spec = {"disks": [{"id": "d1"}]}
        result = asyncio.run(
            _reconcile_disks(
                old_spec, new_spec, "vm-1", "ns", body, MagicMock(), MagicMock(), p
            )
        )
        assert result == {"d1": "pvc-1"}
        mock_provision.assert_called_once()
        mock_delete.assert_called_once()


# ---------------------------------------------------------------------------
# handlers/vm.py — vm_update
# ---------------------------------------------------------------------------


class TestVmUpdate:
    @staticmethod
    def _get_vm_update_fn():
        """Extract the unwrapped vm_update async function from kopf mock."""
        import importlib
        import kopf

        importlib.import_module("handlers.vm")
        decorator_mock = kopf.on.update.return_value
        for call_args in reversed(decorator_mock.call_args_list):
            fn = call_args[0][0]
            if asyncio.iscoroutinefunction(fn) and fn.__name__ == "vm_update":
                return fn
        raise RuntimeError("Could not find vm_update in kopf mock call args")

    @patch("handlers.vm.client")
    def test_noop_when_spec_unchanged(self, mock_client):
        vm_update = self._get_vm_update_fn()

        p = MockPatch()
        old = {"spec": {"disks": []}}
        new = {"spec": {"disks": []}}
        asyncio.run(
            vm_update(
                spec={},
                old=old,
                new=new,
                diff=[],
                status={},
                meta={},
                namespace="ns",
                name="vm-1",
                body={},
                patch=p,
            )
        )
        assert "state" not in p.status

    @patch("handlers.vm._setup_bmc")
    @patch("handlers.vm.build_kubevirt_vm", return_value={"metadata": {"name": "kv-1"}})
    @patch("handlers.vm.owner_ref", return_value={})
    @patch("handlers.vm._upsert_cloudinit_secret", return_value=None)
    @patch("handlers.vm._resolve_nad_refs", return_value={})
    @patch("handlers.vm._delete_and_wait_for_kubevirt_vm")
    @patch("handlers.vm._reconcile_disks", return_value={"d1": "pvc-1"})
    @patch("handlers.vm._stop_kubevirt_vm")
    @patch("handlers.vm.client")
    def test_reconciles_on_spec_change(
        self,
        mock_client,
        mock_stop,
        mock_reconcile,
        mock_del_wait,
        mock_nads,
        mock_ci,
        mock_oref,
        mock_build,
        mock_bmc,
    ):
        vm_update = self._get_vm_update_fn()

        p = MockPatch()
        custom_api = mock_client.CustomObjectsApi.return_value
        custom_api.get_namespaced_custom_object.return_value = {
            "metadata": {"uid": "new-uuid"}
        }
        old = {"spec": {"disks": [{"id": "d1"}], "cpus": 2}}
        new = {"spec": {"disks": [{"id": "d1"}], "cpus": 4}}
        asyncio.run(
            vm_update(
                spec=new["spec"],
                old=old,
                new=new,
                diff=[],
                status={"kubevirtVmName": "kv-1"},
                meta={},
                namespace="ns",
                name="vm-1",
                body={"kind": "TroshkaVM", "metadata": {"name": "vm-1", "uid": "u1"}},
                patch=p,
            )
        )
        mock_stop.assert_called_once()
        mock_reconcile.assert_called_once()
        assert p.status["domainUuid"] == "new-uuid"
        assert p.status["kubevirtVmName"] == "kv-1"


# ---------------------------------------------------------------------------
# helpers/topology.py — _find_storage_vm_pair
# ---------------------------------------------------------------------------


class TestFindStorageVmPair:
    def test_storage_to_vm(self):
        from helpers.topology import _find_storage_vm_pair

        node_map = {
            "s1": {"type": "storageNode", "data": {}},
            "v1": {"type": "vmNode", "data": {}},
        }
        edge = {"source": "s1", "target": "v1"}
        storage_id, vm_id = _find_storage_vm_pair(edge, node_map)
        assert storage_id == "s1"
        assert vm_id == "v1"

    def test_vm_to_storage(self):
        from helpers.topology import _find_storage_vm_pair

        node_map = {
            "s1": {"type": "storageNode", "data": {}},
            "v1": {"type": "vmNode", "data": {}},
        }
        edge = {"source": "v1", "target": "s1"}
        storage_id, vm_id = _find_storage_vm_pair(edge, node_map)
        assert storage_id == "s1"
        assert vm_id == "v1"

    def test_no_match(self):
        from helpers.topology import _find_storage_vm_pair

        node_map = {
            "n1": {"type": "networkNode", "data": {}},
            "n2": {"type": "networkNode", "data": {}},
        }
        edge = {"source": "n1", "target": "n2"}
        storage_id, vm_id = _find_storage_vm_pair(edge, node_map)
        assert storage_id is None
        assert vm_id is None


# ---------------------------------------------------------------------------
# helpers/topology.py — _build_disk_from_storage
# ---------------------------------------------------------------------------


class TestBuildDiskFromStorage:
    def test_blank(self):
        from helpers.topology import _build_disk_from_storage

        sd = {"size": 50}
        result = _build_disk_from_storage(sd, "disk-1")
        assert "disk" in result
        disk = result["disk"]
        assert disk["blank"] is True
        assert disk["id"] == "disk-1"
        assert disk["sizeGb"] == 50
        assert disk["format"] == "qcow2"

    def test_pattern(self):
        from helpers.topology import _build_disk_from_storage

        sd = {
            "source": "pattern",
            "patternId": "pat-1",
            "patternDiskId": "pd-1",
        }
        result = _build_disk_from_storage(sd, "disk-2")
        disk = result["disk"]
        assert "patternImage" in disk
        assert disk["patternImage"]["s3Path"] == "patterns/pat-1/pd-1.qcow2"
        assert disk["patternImage"]["format"] == "qcow2"
        assert disk["patternImage"]["central"] is False
        assert "blank" not in disk

    def test_library(self):
        from helpers.topology import _build_disk_from_storage

        sd = {
            "source": "library",
            "libraryItemId": "lib-1",
            "format": "qcow2",
        }
        result = _build_disk_from_storage(sd, "disk-3")
        disk = result["disk"]
        assert "libraryImage" in disk
        assert disk["libraryImage"]["s3Path"] == "library/lib-1.qcow2"
        assert disk["libraryImage"]["format"] == "qcow2"
        assert "blank" not in disk

    def test_iso_returns_cdrom(self):
        from helpers.topology import _build_disk_from_storage

        sd = {
            "format": "iso",
            "libraryItemId": "iso-1",
        }
        result = _build_disk_from_storage(sd, "disk-4")
        assert "cdrom" in result
        assert "disk" not in result
        assert result["cdrom"]["libraryIsoId"] == "iso-1"
        assert result["cdrom"]["s3Path"] == "library/iso-1.iso"
