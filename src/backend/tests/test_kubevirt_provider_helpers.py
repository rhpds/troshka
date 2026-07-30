"""Tests for kubevirt provider helper functions."""

import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _project_ns — pure string logic
# ---------------------------------------------------------------------------
from app.services.providers.kubevirt import _project_ns


class TestProjectNs:
    def test_default_prefix(self):
        provider = MagicMock()
        provider.get_credentials.return_value = {"api_url": "https://api.example.com"}
        result = _project_ns(provider, "abcdef12-3456-7890-abcd-ef1234567890")
        assert result == "troshka-abcdef12"

    def test_custom_prefix(self):
        provider = MagicMock()
        provider.get_credentials.return_value = {
            "api_url": "https://api.example.com",
            "project_prefix": "myapp-",
        }
        result = _project_ns(provider, "11223344-5566-7788-9900-aabbccddeeff")
        assert result == "myapp-11223344"

    def test_truncates_project_id_to_8_chars(self):
        provider = MagicMock()
        provider.get_credentials.return_value = {}
        result = _project_ns(provider, "abcdefgh-ijkl")
        assert result == "troshka-abcdefgh"


# ---------------------------------------------------------------------------
# _stop_vms_gracefully — mock K8s API
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _stop_vms_gracefully


class TestStopVmsGracefully:
    def test_patches_running_false_on_each_vm(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "vm-1"}},
                {"metadata": {"name": "vm-2"}},
            ]
        }
        _stop_vms_gracefully(custom_api, "ns-test")

        assert custom_api.patch_namespaced_custom_object.call_count == 2
        for c in custom_api.patch_namespaced_custom_object.call_args_list:
            assert c.kwargs["body"] == {"spec": {"running": False}}

    def test_handles_list_exception(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("API down")
        # Should not raise
        _stop_vms_gracefully(custom_api, "ns-test")

    def test_handles_patch_exception(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "vm-1"}}]
        }
        custom_api.patch_namespaced_custom_object.side_effect = Exception("Forbidden")
        # Should not raise
        _stop_vms_gracefully(custom_api, "ns-test")

    def test_handles_empty_items(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {"items": []}
        _stop_vms_gracefully(custom_api, "ns-test")
        custom_api.patch_namespaced_custom_object.assert_not_called()


# ---------------------------------------------------------------------------
# _force_delete_vmis — mock K8s API
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _force_delete_vmis


class TestForceDeleteVmis:
    def test_deletes_all_vmis_with_grace_period_zero(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "vmi-1"}},
                {"metadata": {"name": "vmi-2"}},
            ]
        }
        _force_delete_vmis(custom_api, "ns-test")

        assert custom_api.delete_namespaced_custom_object.call_count == 2
        for c in custom_api.delete_namespaced_custom_object.call_args_list:
            assert c.kwargs["grace_period_seconds"] == 0

    def test_handles_empty_list(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {"items": []}
        _force_delete_vmis(custom_api, "ns-test")
        custom_api.delete_namespaced_custom_object.assert_not_called()

    def test_handles_list_exception(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("err")
        _force_delete_vmis(custom_api, "ns-test")


# ---------------------------------------------------------------------------
# _force_delete_virt_launcher_pods — mock K8s API
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _force_delete_virt_launcher_pods


class TestForceDeleteVirtLauncherPods:
    def test_deletes_virt_launcher_pods(self):
        core_api = MagicMock()
        pod1 = MagicMock()
        pod1.metadata.name = "virt-launcher-vm1-abc"
        pod2 = MagicMock()
        pod2.metadata.name = "virt-launcher-vm2-def"
        result = MagicMock()
        result.items = [pod1, pod2]
        core_api.list_namespaced_pod.return_value = result

        _force_delete_virt_launcher_pods(core_api, "ns-test")

        assert core_api.delete_namespaced_pod.call_count == 2
        core_api.delete_namespaced_pod.assert_any_call(
            name="virt-launcher-vm1-abc",
            namespace="ns-test",
            grace_period_seconds=0,
        )

    def test_handles_list_exception(self):
        core_api = MagicMock()
        core_api.list_namespaced_pod.side_effect = Exception("err")
        _force_delete_virt_launcher_pods(core_api, "ns-test")

    def test_handles_delete_exception(self):
        core_api = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "virt-launcher-x"
        result = MagicMock()
        result.items = [pod]
        core_api.list_namespaced_pod.return_value = result
        core_api.delete_namespaced_pod.side_effect = Exception("Forbidden")
        # Should not raise
        _force_delete_virt_launcher_pods(core_api, "ns-test")


# ---------------------------------------------------------------------------
# _collect_pv_names — mock K8s API
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _collect_pv_names


class TestCollectPvNames:
    def test_collects_pv_names_from_pvcs(self):
        core_api = MagicMock()
        pvc1 = MagicMock()
        pvc1.spec.volume_name = "pv-001"
        pvc2 = MagicMock()
        pvc2.spec.volume_name = "pv-002"
        pvc3 = MagicMock()
        pvc3.spec.volume_name = None  # unbound PVC
        result = MagicMock()
        result.items = [pvc1, pvc2, pvc3]
        core_api.list_namespaced_persistent_volume_claim.return_value = result

        pv_names = _collect_pv_names(core_api, "ns-test")

        assert pv_names == {"pv-001", "pv-002"}

    def test_returns_empty_set_when_no_pvcs(self):
        core_api = MagicMock()
        result = MagicMock()
        result.items = []
        core_api.list_namespaced_persistent_volume_claim.return_value = result

        pv_names = _collect_pv_names(core_api, "ns-test")
        assert pv_names == set()


# ---------------------------------------------------------------------------
# _delete_detached_volume_attachments — mock K8s API
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _delete_detached_volume_attachments


class TestDeleteDetachedVolumeAttachments:
    def test_deletes_only_detached(self):
        storage_api = MagicMock()

        va_detached = MagicMock()
        va_detached.status.attached = False
        va_detached.metadata.name = "va-detached"

        va_attached = MagicMock()
        va_attached.status.attached = True
        va_attached.metadata.name = "va-attached"

        _delete_detached_volume_attachments(storage_api, [va_detached, va_attached])

        storage_api.delete_volume_attachment.assert_called_once_with(name="va-detached")

    def test_handles_missing_status(self):
        storage_api = MagicMock()
        va = MagicMock()
        va.status = None  # no status attribute
        _delete_detached_volume_attachments(storage_api, [va])
        # attached defaults to True when status is None, so no delete
        storage_api.delete_volume_attachment.assert_not_called()

    def test_handles_delete_exception(self):
        storage_api = MagicMock()
        va = MagicMock()
        va.status.attached = False
        va.metadata.name = "va-fail"
        storage_api.delete_volume_attachment.side_effect = Exception("err")
        # Should not raise
        _delete_detached_volume_attachments(storage_api, [va])


# ---------------------------------------------------------------------------
# _delete_vm_crs — mock K8s API
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _delete_vm_crs


class TestDeleteVmCrs:
    def test_deletes_all_vm_crs(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "vm-a"}},
                {"metadata": {"name": "vm-b"}},
            ]
        }
        _delete_vm_crs(custom_api, "ns-test")

        assert custom_api.delete_namespaced_custom_object.call_count == 2
        custom_api.delete_namespaced_custom_object.assert_any_call(
            group="kubevirt.io",
            version="v1",
            namespace="ns-test",
            plural="virtualmachines",
            name="vm-a",
            grace_period_seconds=0,
        )

    def test_handles_list_exception(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("err")
        _delete_vm_crs(custom_api, "ns-test")

    def test_handles_empty_items(self):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {"items": []}
        _delete_vm_crs(custom_api, "ns-test")
        custom_api.delete_namespaced_custom_object.assert_not_called()


# ---------------------------------------------------------------------------
# _delete_namespace_jobs — mock K8s API
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _delete_namespace_jobs


class TestDeleteNamespaceJobs:
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_deletes_all_jobs(self, mock_clients):
        provider = MagicMock()
        mock_api_client = MagicMock()
        mock_clients.return_value = (MagicMock(), MagicMock(), mock_api_client)

        job1 = MagicMock()
        job1.metadata.name = "recert-job"
        job2 = MagicMock()
        job2.metadata.name = "export-job"

        with patch("kubernetes.client.BatchV1Api") as mock_batch_cls:
            mock_batch = MagicMock()
            mock_batch.list_namespaced_job.return_value = MagicMock(items=[job1, job2])
            mock_batch_cls.return_value = mock_batch

            _delete_namespace_jobs(provider, "ns-test")

            assert mock_batch.delete_namespaced_job.call_count == 2
            mock_batch.delete_namespaced_job.assert_any_call(
                name="recert-job",
                namespace="ns-test",
                propagation_policy="Background",
            )

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_handles_exception(self, mock_clients):
        mock_clients.side_effect = Exception("connection refused")
        _delete_namespace_jobs(MagicMock(), "ns-test")  # should not raise


# ---------------------------------------------------------------------------
# _find_exec_pod — mock K8s API
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _find_exec_pod


class TestFindExecPod:
    def test_returns_running_exec_pod(self):
        core_v1 = MagicMock()
        exec_pod = MagicMock()
        exec_pod.status.phase = "Running"

        exec_result = MagicMock()
        exec_result.items = [exec_pod]
        core_v1.list_namespaced_pod.return_value = exec_result

        result = _find_exec_pod(core_v1, "ns-test", "abcdef12-3456-7890")
        assert result is exec_pod

    def test_falls_back_to_dnsmasq_pod(self):
        core_v1 = MagicMock()

        # First call (exec pods) returns no running pods
        exec_pod = MagicMock()
        exec_pod.status.phase = "Pending"
        exec_result = MagicMock()
        exec_result.items = [exec_pod]

        # Second call (dnsmasq pods) returns running pod
        dns_pod = MagicMock()
        dns_pod.status.phase = "Running"
        dns_result = MagicMock()
        dns_result.items = [dns_pod]

        core_v1.list_namespaced_pod.side_effect = [exec_result, dns_result]

        result = _find_exec_pod(core_v1, "ns-test", "abcdef12-3456-7890")
        assert result is dns_pod

    def test_returns_none_when_no_pods(self):
        core_v1 = MagicMock()
        empty_result = MagicMock()
        empty_result.items = []
        core_v1.list_namespaced_pod.return_value = empty_result

        result = _find_exec_pod(core_v1, "ns-test", "abcdef12-3456-7890")
        assert result is None


# ---------------------------------------------------------------------------
# _wait_for_vmis_terminated — mock K8s API + time
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _wait_for_vmis_terminated


class TestWaitForVmisTerminated:
    @patch("time.sleep", return_value=None)
    def test_returns_when_vmis_gone(self, _sleep):
        custom_api = MagicMock()
        # First call: VMIs still exist; second call: empty
        custom_api.list_namespaced_custom_object.side_effect = [
            {"items": [{"metadata": {"name": "vmi-1"}}]},
            {"items": []},
        ]
        _wait_for_vmis_terminated(custom_api, "ns-test")
        assert custom_api.list_namespaced_custom_object.call_count == 2

    @patch("time.sleep", return_value=None)
    def test_breaks_on_exception(self, _sleep):
        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.side_effect = Exception("gone")
        _wait_for_vmis_terminated(custom_api, "ns-test")
        assert custom_api.list_namespaced_custom_object.call_count == 1


# ---------------------------------------------------------------------------
# _wait_for_virt_launchers_gone — mock K8s API + time
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import _wait_for_virt_launchers_gone


class TestWaitForVirtLaunchersGone:
    @patch("time.sleep", return_value=None)
    def test_returns_when_both_empty(self, _sleep):
        custom_api = MagicMock()
        core_api = MagicMock()

        # Pods still exist first, then both empty
        pod_result_with = MagicMock()
        pod_result_with.items = [MagicMock()]
        pod_result_empty = MagicMock()
        pod_result_empty.items = []

        core_api.list_namespaced_pod.side_effect = [pod_result_with, pod_result_empty]
        custom_api.list_namespaced_custom_object.side_effect = [
            {"items": [{"metadata": {"name": "vmi-1"}}]},
            {"items": []},
        ]

        _wait_for_virt_launchers_gone(custom_api, core_api, "ns-test")
        assert core_api.list_namespaced_pod.call_count == 2


# ---------------------------------------------------------------------------
# KubeVirtDriver.destroy_project — integration of helpers
# ---------------------------------------------------------------------------

from app.services.providers.kubevirt import KubeVirtDriver


class TestKubeVirtDriverDestroyProject:
    @patch("app.services.providers.kubevirt._delete_namespace_jobs")
    @patch("app.services.providers.kubevirt._delete_vm_crs")
    @patch("app.services.providers.kubevirt._cleanup_volume_attachments")
    @patch("app.services.providers.kubevirt._wait_for_virt_launchers_gone")
    @patch("app.services.providers.kubevirt._force_delete_virt_launcher_pods")
    @patch("app.services.providers.kubevirt._force_delete_vmis")
    @patch("app.services.providers.kubevirt._wait_for_vmis_terminated")
    @patch("app.services.providers.kubevirt._stop_vms_gracefully")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch("app.services.providers.kubevirt._project_ns")
    @patch("time.sleep", return_value=None)
    def test_calls_all_cleanup_steps_in_order(
        self,
        _sleep,
        mock_ns,
        mock_clients,
        mock_stop,
        mock_wait_vmis,
        mock_force_vmis,
        mock_force_pods,
        mock_wait_launchers,
        mock_cleanup_va,
        mock_delete_vms,
        mock_delete_jobs,
    ):
        mock_ns.return_value = "troshka-abcdef12"
        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_clients.return_value = (mock_custom, mock_core, MagicMock())

        # get returns 404 (project CR already gone) to skip wait loop
        mock_custom.get_namespaced_custom_object.side_effect = Exception("NotFound")

        provider = MagicMock()
        driver = KubeVirtDriver()
        driver.destroy_project(provider, "abcdef12-3456-7890-abcd-ef1234567890")

        mock_stop.assert_called_once_with(mock_custom, "troshka-abcdef12")
        mock_wait_vmis.assert_called_once_with(mock_custom, "troshka-abcdef12")
        mock_force_vmis.assert_called_once_with(mock_custom, "troshka-abcdef12")
        mock_force_pods.assert_called_once_with(mock_core, "troshka-abcdef12")
        mock_wait_launchers.assert_called_once_with(
            mock_custom, mock_core, "troshka-abcdef12"
        )
        mock_cleanup_va.assert_called_once_with(mock_core, "troshka-abcdef12")
        mock_delete_vms.assert_called_once_with(mock_custom, "troshka-abcdef12")
        mock_delete_jobs.assert_called_once_with(provider, "troshka-abcdef12")

        # Verify namespace deletion attempted
        mock_core.delete_namespace.assert_called_once_with(name="troshka-abcdef12")

        # Verify CR deletion attempted
        mock_custom.delete_namespaced_custom_object.assert_called_once()


# ---------------------------------------------------------------------------
# KubeVirtDriver simple methods
# ---------------------------------------------------------------------------


class TestKubeVirtDriverSimpleMethods:
    def test_terminate_host_is_noop(self):
        driver = KubeVirtDriver()
        driver.terminate_host(MagicMock(), "instance-1")  # should not raise

    def test_resize_host_returns_empty(self):
        driver = KubeVirtDriver()
        assert driver.resize_host(MagicMock(), "instance-1", "new-type") == {}

    def test_extend_host_storage_returns_empty(self):
        driver = KubeVirtDriver()
        assert driver.extend_host_storage(MagicMock(), MagicMock(), MagicMock()) == {}

    def test_get_host_powerstate_returns_running(self):
        driver = KubeVirtDriver()
        assert driver.get_host_powerstate(MagicMock(), "instance-1") == "running"

    def test_start_host_is_noop(self):
        driver = KubeVirtDriver()
        driver.start_host(MagicMock(), "instance-1")

    def test_stop_host_is_noop(self):
        driver = KubeVirtDriver()
        driver.stop_host(MagicMock(), "instance-1")

    def test_setup_console_returns_domain_info(self):
        driver = KubeVirtDriver()
        result = driver.setup_console(MagicMock(), "console.example.com")
        assert result["console_base_domain"] == "console.example.com"
        assert result["console_zone_id"] == ""
        assert result["console_nameservers"] == []

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_get_host_status_returns_running(self, mock_clients):
        mock_core = MagicMock()
        mock_clients.return_value = (MagicMock(), mock_core, MagicMock())
        provider = MagicMock()
        provider.get_credentials.return_value = {"namespace": "troshka"}

        driver = KubeVirtDriver()
        result = driver.get_host_status(
            provider, "https://api.cluster.example.com:6443"
        )
        assert result["state"] == "running"
        assert result["instance_id"] == "https://api.cluster.example.com:6443"

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_get_host_status_returns_none_on_failure(self, mock_clients):
        mock_core = MagicMock()
        mock_core.read_namespace.side_effect = Exception("Not found")
        mock_clients.return_value = (MagicMock(), mock_core, MagicMock())
        provider = MagicMock()
        provider.get_credentials.return_value = {"namespace": "troshka"}

        driver = KubeVirtDriver()
        result = driver.get_host_status(
            provider, "https://api.cluster.example.com:6443"
        )
        assert result is None
