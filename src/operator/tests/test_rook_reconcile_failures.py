"""Exercise API failure and retry boundaries in namespace-scoped Ceph setup."""

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from helpers import rook_ceph as rook


@pytest.fixture
def ceph_cr():
    return {
        "apiVersion": "troshka.io/v1alpha1",
        "kind": "TroshkaCeph",
        "metadata": {"name": "troshka-ceph", "namespace": "lab", "uid": "ceph-uid"},
        "spec": {"osdCount": 1, "capacityGi": 50, "labIp": "10.0.0.4"},
    }


def exported_secret(mon_host="192.0.2.4:3300"):
    return SimpleNamespace(data={
        "external_cluster_details": "exported",
        "mon-host": base64.b64encode(mon_host.encode()).decode(),
    })


@pytest.mark.parametrize("status", [404, 403, 503])
def test_image_discovery_uses_fallback_when_cluster_images_unavailable(status):
    api = MagicMock()
    api.read_namespaced_deployment.side_effect = ApiException(status=status)
    api.list_namespaced_custom_object.side_effect = ApiException(status=status)
    assert rook.discover_rook_operator_image(api) == rook.DEFAULT_ROOK_OPERATOR_IMAGE
    assert rook.discover_ceph_image(api) == "quay.io/ceph/ceph:v19"


@pytest.mark.parametrize("existing", [False, True])
def test_operator_reconcile_creates_or_updates_owned_resources(ceph_cr, existing):
    core, apps, rbac = MagicMock(), MagicMock(), MagicMock()
    apps.read_namespaced_deployment.return_value.spec.template.spec.containers = [
        SimpleNamespace(image="registry.example/rook:cluster")
    ]
    if existing:
        core.create_namespaced_config_map.side_effect = ApiException(status=409)
        core.create_namespaced_service_account.side_effect = ApiException(status=409)
        apps.create_namespaced_deployment.side_effect = ApiException(status=409)
        rbac.create_cluster_role_binding.side_effect = ApiException(status=409)
        rbac.create_namespaced_role.side_effect = ApiException(status=409)
        rbac.create_namespaced_role_binding.side_effect = ApiException(status=409)
    with (
        patch.object(rook.client, "CoreV1Api", return_value=core),
        patch.object(rook.client, "AppsV1Api", return_value=apps),
        patch.object(rook.client, "RbacAuthorizationV1Api", return_value=rbac),
    ):
        rook.ensure_rook_operator(ceph_cr)
    deployment = apps.create_namespaced_deployment.call_args.kwargs["body"]
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "registry.example/rook:cluster"
    assert core.create_namespaced_service_account.call_count == len(rook.ROOK_OPERAND_SAS)
    assert rbac.create_namespaced_role.call_count == 1
    assert rbac.create_namespaced_role_binding.call_count == 1
    assert apps.patch_namespaced_deployment.call_count == int(existing)
    assert core.patch_namespaced_config_map.call_count == int(existing)
    if existing:
        assert apps.patch_namespaced_deployment.call_args.kwargs["body"] == deployment


@pytest.mark.parametrize("helper,create_method", [
    (rook._create_or_patch_configmap, "create_namespaced_config_map"),
    (rook._create_or_patch_deployment, "create_namespaced_deployment"),
])
def test_update_helpers_propagate_forbidden_instead_of_patching(helper, create_method):
    api = MagicMock()
    getattr(api, create_method).side_effect = ApiException(status=403)
    body = {"metadata": {"name": "owned"}}
    with pytest.raises(ApiException) as exc:
        helper(api, "lab", body)
    assert exc.value.status == 403
    assert len(api.mock_calls) == 1


def test_operator_rbac_helpers_propagate_permission_errors(ceph_cr):
    api = MagicMock()
    api.create_cluster_role_binding.side_effect = ApiException(status=403)
    api.create_namespaced_service_account.side_effect = ApiException(status=403)
    api.create_namespaced_role.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        rook._create_cluster_role_binding(api, {"metadata": {"name": "owned"}})
    with pytest.raises(ApiException):
        rook._ensure_service_account(api, "lab", "rook", ceph_cr)
    with pytest.raises(ApiException):
        rook._ensure_rook_namespace_rbac(api, "lab", ceph_cr)
    api.create_namespaced_role_binding.assert_not_called()


def test_export_refresh_clears_stale_lab_address_and_relaunches_job(ceph_cr):
    core, batch = MagicMock(), MagicMock()
    core.read_namespaced_secret.return_value = exported_secret("10.0.0.4:3300")
    batch.read_namespaced_job.side_effect = ApiException(status=404)
    with patch.object(rook.client, "CoreV1Api", return_value=core):
        rook.ensure_ceph_export_job(batch, ceph_cr, "lab")
    assert core.patch_namespaced_secret.call_args.kwargs["body"] == {
        "data": {"external_cluster_details": None}
    }
    batch.delete_namespaced_job.assert_called_once()
    job = batch.create_namespaced_job.call_args.kwargs["body"]
    assert job["metadata"]["name"] == rook.EXPORT_JOB_NAME
    assert job["metadata"]["ownerReferences"][0]["uid"] == "ceph-uid"


def test_export_reconcile_leaves_current_host_network_export_untouched(ceph_cr):
    core, batch = MagicMock(), MagicMock()
    core.read_namespaced_secret.return_value = exported_secret()
    with patch.object(rook.client, "CoreV1Api", return_value=core):
        rook.ensure_ceph_export_job(batch, ceph_cr, "lab")
    core.patch_namespaced_secret.assert_not_called()
    assert batch.mock_calls == []


@pytest.mark.parametrize("job_state,create_count", [("running", 0), ("failed", 1), ("succeeded", 1)])
def test_missing_export_retries_completed_jobs_but_waits_for_running_job(ceph_cr, job_state, create_count):
    core, batch = MagicMock(), MagicMock()
    core.read_namespaced_secret.side_effect = ApiException(status=404)
    batch.read_namespaced_job.return_value = SimpleNamespace(status=SimpleNamespace(
        failed=int(job_state == "failed"), succeeded=int(job_state == "succeeded")
    ))
    with patch.object(rook.client, "CoreV1Api", return_value=core):
        rook.ensure_ceph_export_job(batch, ceph_cr, "lab")
    assert batch.create_namespaced_job.call_count == create_count
    assert batch.delete_namespaced_job.call_count == create_count
    core.patch_namespaced_secret.assert_not_called()


@pytest.mark.parametrize("helper,method", [
    (rook._delete_export_job, "delete_namespaced_job"),
    (rook._read_export_job, "read_namespaced_job"),
    (rook.clear_ceph_external_details, "patch_namespaced_secret"),
])
def test_export_api_operations_ignore_absence_but_propagate_outage(helper, method):
    api = MagicMock()
    getattr(api, method).side_effect = ApiException(status=404)
    assert helper(api, "lab") is None
    getattr(api, method).side_effect = ApiException(status=503)
    with pytest.raises(ApiException) as exc:
        helper(api, "lab")
    assert exc.value.status == 503


@pytest.mark.parametrize("value,expected", [(None, 0), ("", 0), ("2Ti", 2 * 1024**4),
    ("2Gi", 2 * 1024**3), ("8Mi", 8 * 1024**2), ("1.5G", 1500000000),
    ("512", 512), ("unknown", 0)])
def test_capture_storage_sizes_preserve_supported_units(value, expected):
    assert rook._storage_request_bytes(value) == expected


def test_osd_count_uses_rook_cluster_for_legacy_project():
    api = MagicMock()
    api.get_namespaced_custom_object.side_effect = [ApiException(status=404), {
        "spec": {"storage": {"storageClassDeviceSets": [{"count": 3}]}}
    }]
    assert rook._expected_osd_count("lab", api) == 3
    assert api.get_namespaced_custom_object.call_args.kwargs["plural"] == "cephclusters"


@pytest.mark.parametrize("legacy", [ApiException(status=404), {"spec": {}}])
def test_osd_capture_refuses_missing_storage_inventory(legacy):
    api = MagicMock()
    api.get_namespaced_custom_object.side_effect = [ApiException(status=404), legacy]
    with pytest.raises(ValueError):
        rook._expected_osd_count("lab", api)


@pytest.mark.parametrize("responses", [[ApiException(status=403)], [ApiException(status=404), ApiException(status=503)]])
def test_osd_capture_propagates_api_errors_instead_of_assuming_empty(responses):
    api = MagicMock()
    api.get_namespaced_custom_object.side_effect = responses
    with pytest.raises(ApiException):
        rook._expected_osd_count("lab", api)


def test_stuck_cluster_cleanup_attempts_all_resources_even_during_api_failure(caplog):
    custom, core = MagicMock(), MagicMock()
    custom.delete_namespaced_custom_object.side_effect = ApiException(status=503)
    custom.get_namespaced_custom_object.side_effect = ApiException(status=503)
    custom.patch_namespaced_custom_object.side_effect = ApiException(status=403)
    core.list_namespaced_secret.side_effect = ApiException(status=403)
    protected = SimpleNamespace(metadata=SimpleNamespace(
        name="rook-mon", finalizers=["ceph.rook.io/disaster-protection"]
    ))
    unrelated = SimpleNamespace(metadata=SimpleNamespace(name="app", finalizers=[]))
    core.list_namespaced_config_map.return_value = SimpleNamespace(items=[unrelated, protected])
    core.patch_namespaced_config_map.side_effect = ApiException(status=403)
    rook.delete_rook_ceph_crs(custom, core, "lab", wait_seconds=0)
    assert custom.delete_namespaced_custom_object.call_count == 4
    assert custom.patch_namespaced_custom_object.call_count == 2
    assert core.patch_namespaced_config_map.call_args.kwargs["name"] == "rook-mon"
    assert "Poll cephclusters/troshka-ceph failed" in caplog.text
    assert "Re-delete cephclusters/troshka-ceph failed" in caplog.text


@pytest.mark.parametrize("patch_status,delete_status", [(404, 404), (409, 503), (403, 403)])
def test_pvc_cleanup_attempts_delete_even_if_finalizer_patch_fails(patch_status, delete_status):
    core = MagicMock()
    core.patch_namespaced_persistent_volume_claim.side_effect = ApiException(status=patch_status)
    core.delete_namespaced_persistent_volume_claim.side_effect = ApiException(status=delete_status)
    seen = set()
    rook._delete_ceph_pvc(core, "lab", "troshka-ceph-osd-0", seen)
    rook._delete_ceph_pvc(core, "lab", "troshka-ceph-osd-0", seen)
    rook._delete_ceph_pvc(core, "lab", "", seen)
    core.delete_namespaced_persistent_volume_claim.assert_called_once()
    assert seen == {"troshka-ceph-osd-0"}


@pytest.mark.parametrize("status", [404, 403])
def test_pvc_cleanup_tries_all_selectors_and_prefix_after_list_errors(status):
    core = MagicMock()
    core.list_namespaced_persistent_volume_claim.side_effect = ApiException(status=status)
    rook.delete_ceph_storage_pvcs(core, "lab")
    assert core.list_namespaced_persistent_volume_claim.call_count == 3
    core.delete_namespaced_persistent_volume_claim.assert_not_called()


@pytest.mark.parametrize("kind,method", [("Secret", "patch_namespaced_secret"), ("ConfigMap", "patch_namespaced_config_map")])
def test_disaster_finalizer_can_be_removed_from_core_resources(kind, method):
    core, custom = MagicMock(), MagicMock()
    rook._strip_object_finalizers(custom, core, "lab", kind, "rook-mon")
    getattr(core, method).assert_called_once_with(
        name="rook-mon", namespace="lab", body={"metadata": {"finalizers": None}}
    )
    custom.patch_namespaced_custom_object.assert_not_called()
