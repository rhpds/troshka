"""Tests for Project Ceph rook manifest helpers."""

from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from helpers.rook_ceph import (
    CEPH_EXTERNAL_SECRET,
    TROSHKA_ROOK_SA_CLUSTER_ROLES,
    build_ceph_cluster,
    build_ceph_rbac,
    build_export_job,
    build_external_secret,
    build_rook_operator_config,
    build_rook_operator_deployment,
    ceph_export_needs_nested_mon_refresh,
    data_dir_host_path,
    default_lab_ip_from_cidr,
    discover_ceph_device_pvcs,
    discover_ceph_image,
    normalize_ceph_counts,
    rook_crb_name,
    validate_lab_ip,
)


def _mock_pvc(name: str, storage: str, labels: Optional[dict] = None):
    pvc = MagicMock()
    pvc.metadata.name = name
    pvc.metadata.labels = labels or {}
    pvc.spec.resources.requests = {"storage": storage}
    return pvc


def _mock_troshka_ceph_osd_count(osd_count: int):
    custom_api = MagicMock()
    custom_api.get_namespaced_custom_object.return_value = {
        "spec": {"osdCount": osd_count, "capacityGi": osd_count * 50}
    }
    return custom_api


def test_default_lab_ip_from_cidr():
    assert default_lab_ip_from_cidr("10.0.0.0/24") == "10.0.0.4"
    assert default_lab_ip_from_cidr("") == ""


def test_normalize_ceph_counts_clamps():
    osd, repl, per = normalize_ceph_counts({"osdCount": 9, "capacityGi": 100})
    assert osd == 6
    assert repl == 3
    assert per >= 50


def test_build_ceph_cluster_omits_removed_rook_fields():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.3", "capacityGi": 300, "osdCount": 3},
    }
    cluster = build_ceph_cluster(cr)
    assert "removeOSDsIfOutOfSafeRange" not in cluster["spec"]
    assert "cleanupPolicy" not in cluster["spec"]


def test_build_ceph_cluster_mons_use_pvcs_not_shared_odf_path():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.3", "capacityGi": 300, "osdCount": 3},
    }
    cluster = build_ceph_cluster(cr)
    mon = cluster["spec"]["mon"]
    vct = mon["volumeClaimTemplate"]["spec"]
    assert vct["accessModes"] == ["ReadWriteOnce"]
    assert vct["resources"]["requests"]["storage"] == "10Gi"
    assert cluster["spec"]["dataDirHostPath"] == "/var/lib/rook-troshka-abc"
    assert cluster["spec"]["dataDirHostPath"] != "/var/lib/rook"


def test_build_ceph_cluster_device_sets():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {
            "labIp": "10.0.0.3",
            "capacityGi": 300,
            "osdCount": 3,
        },
    }
    cluster = build_ceph_cluster(cr)
    sets = cluster["spec"]["storage"]["storageClassDeviceSets"]
    assert sets[0]["count"] == 3


def test_validate_lab_ip():
    assert validate_lab_ip("10.0.0.3")
    assert not validate_lab_ip("not-an-ip")


def test_data_dir_host_path_is_unique_per_project():
    assert data_dir_host_path("troshka-abc") == "/var/lib/rook-troshka-abc"
    assert data_dir_host_path("troshka-abc") != "/var/lib/rook"


def test_build_ceph_cluster_avoids_odf_storage_nodes():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.3", "capacityGi": 300, "osdCount": 3},
    }
    cluster = build_ceph_cluster(cr)
    affinity = cluster["spec"]["placement"]["all"]["nodeAffinity"]
    expr = affinity["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ][0]["matchExpressions"][0]
    assert expr["key"] == "cluster.ocs.openshift.io/openshift-storage"
    assert expr["operator"] == "DoesNotExist"


def test_build_rook_operator_scoped_to_namespace():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.3"},
    }
    dep = build_rook_operator_deployment(cr)
    env = {
        e["name"]: e.get("value")
        for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["ROOK_CURRENT_NAMESPACE_ONLY"] == "true"
    assert env["ROOK_CSI_DISABLE_DRIVER"] == "true"
    assert env["ROOK_HOSTPATH_REQUIRES_PRIVILEGED"] == "true"
    assert env["ROOK_CEPH_MON_RUN_AS_ROOT"] == "true"
    assert dep["spec"]["template"]["spec"]["serviceAccountName"] == "rook-ceph-system"


def test_build_rook_operator_config_openshift_privileged_mons():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.3"},
    }
    data = build_rook_operator_config(cr)["data"]
    assert data["ROOK_HOSTPATH_REQUIRES_PRIVILEGED"] == "true"
    assert data["ROOK_CEPH_MON_RUN_AS_ROOT"] == "true"


def test_discover_ceph_image_from_odf():
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {
        "items": [
            {"spec": {"cephVersion": {"image": "registry.example/ceph@sha256:abc"}}}
        ]
    }
    assert discover_ceph_image(api) == "registry.example/ceph@sha256:abc"


def test_build_ceph_cluster_uses_discovered_image():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.3", "capacityGi": 300, "osdCount": 3},
    }
    cluster = build_ceph_cluster(cr, ceph_image="registry.example/ceph@sha256:abc")
    assert cluster["spec"]["cephVersion"]["image"] == "registry.example/ceph@sha256:abc"


def test_rook_osd_sa_has_cluster_mgmt_binding():
    assert "troshka-rook-cluster-mgmt" in TROSHKA_ROOK_SA_CLUSTER_ROLES["rook-ceph-osd"]


def test_rook_crb_name_fits_k8s_limit():
    name = rook_crb_name(
        "troshka-b57b2bab-3773-4086-b33b-ebe2ecf46676",
        "rook-ceph-system",
        "troshka-rook-global",
    )
    assert len(name) <= 63


def test_build_rook_operator_has_pod_name_env():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.3"},
    }
    env_names = {
        e["name"]
        for e in build_rook_operator_deployment(cr)["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]
    }
    assert {"POD_NAME", "POD_NAMESPACE", "NODE_NAME"} <= env_names


def test_build_export_job_writes_odf_external_cluster_details():
    from helpers.k8s import TOOLS_IMAGE

    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.4"},
    }
    job = build_export_job(cr)
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == TOOLS_IMAGE
    script = container["command"][-1]
    assert "external_cluster_details" in script
    assert "rook-ceph-mon-endpoints" in script
    assert "a=" in script  # rook expects id=host:port mon endpoint format
    assert "rook-ceph-admin-keyring" in script
    assert "ceph_exec" not in script
    # Nested CSI path: hostNetwork mon on msgr2, single monmap (not dual labIp).
    assert "discover_hostnetwork_mon_ip" in script
    assert "collapse_monmap_to_host" in script
    assert "mon set-addrs" in script
    assert ":3300" in script
    assert "mon-host-lab" in script


def test_build_external_secret_placeholder_uses_msgr2():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.4"},
    }
    secret = build_external_secret(cr, fsid="fsid-1")
    assert secret["metadata"]["name"] == CEPH_EXTERNAL_SECRET
    assert secret["stringData"]["mon-host"] == "10.0.0.4:3300"


def test_ceph_export_needs_refresh_when_mon_is_lab_bridge():
    import base64

    api = MagicMock()
    secret = MagicMock()
    secret.data = {
        "external_cluster_details": base64.b64encode(b"[]").decode(),
        "mon-host": base64.b64encode(b"10.0.0.4:3300").decode(),
    }
    api.read_namespaced_secret.return_value = secret
    assert ceph_export_needs_nested_mon_refresh(api, "troshka-abc", "10.0.0.4")


def test_ceph_export_ok_when_hostnetwork_msgr2():
    import base64

    api = MagicMock()
    secret = MagicMock()
    secret.data = {
        "external_cluster_details": base64.b64encode(b"[]").decode(),
        "mon-host": base64.b64encode(b"192.168.50.10:3300").decode(),
    }
    api.read_namespaced_secret.return_value = secret
    assert not ceph_export_needs_nested_mon_refresh(api, "troshka-abc", "10.0.0.4")


def test_build_ceph_rbac_allows_export_job_secret_access():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {"labIp": "10.0.0.4"},
    }
    role, _binding = build_ceph_rbac(cr)
    rules = {tuple(rule["resources"]): rule["verbs"] for rule in role["rules"]}
    assert rules[("secrets",)] == ["get", "patch", "create", "update"]
    assert rules[("cephclusters",)] == ["get"]


def test_discover_ceph_device_pvcs_spike_names():
    namespace = "troshka-spike-rookadopt"
    mon = _mock_pvc("rook-ceph-mon-a", "10Gi")
    osd = _mock_pvc(
        "osd-set-data-06p6rg",
        "150Gi",
        labels={
            "ceph.rook.io/DeviceSet": "osd-set",
            "ceph.rook.io/DeviceSetPVCId": "osd-set-data-0",
            "ceph.rook.io/setIndex": "0",
        },
    )
    core_api = MagicMock()
    core_api.read_namespaced_persistent_volume_claim.return_value = mon
    core_api.list_namespaced_persistent_volume_claim.return_value = MagicMock(items=[osd])

    with patch(
        "helpers.rook_ceph.client.CustomObjectsApi",
        return_value=_mock_troshka_ceph_osd_count(1),
    ):
        devices = discover_ceph_device_pvcs(core_api, namespace)

    assert devices == [
        {
            "name": "rook-ceph-mon-a",
            "kind": "ceph-mon",
            "index": 0,
            "size_bytes": 10 * 1073741824,
        },
        {
            "name": "osd-set-data-06p6rg",
            "kind": "ceph-osd",
            "index": 0,
            "size_bytes": 150 * 1073741824,
        },
    ]


def test_discover_ceph_device_pvcs_three_osds():
    namespace = "troshka-abc"
    mon = _mock_pvc("rook-ceph-mon-a", "10Gi")
    osds = [
        _mock_pvc(
            "osd-set-data-07ln8j",
            "100Gi",
            labels={
                "ceph.rook.io/DeviceSet": "osd-set",
                "ceph.rook.io/DeviceSetPVCId": "osd-set-data-0",
                "ceph.rook.io/setIndex": "0",
            },
        ),
        _mock_pvc(
            "osd-set-data-184c79",
            "100Gi",
            labels={
                "ceph.rook.io/DeviceSet": "osd-set",
                "ceph.rook.io/DeviceSetPVCId": "osd-set-data-1",
                "ceph.rook.io/setIndex": "1",
            },
        ),
        _mock_pvc(
            "osd-set-data-24m8n7",
            "100Gi",
            labels={
                "ceph.rook.io/DeviceSet": "osd-set",
                "ceph.rook.io/DeviceSetPVCId": "osd-set-data-2",
                "ceph.rook.io/setIndex": "2",
            },
        ),
    ]
    core_api = MagicMock()
    core_api.read_namespaced_persistent_volume_claim.return_value = mon
    core_api.list_namespaced_persistent_volume_claim.return_value = MagicMock(items=osds)

    with patch(
        "helpers.rook_ceph.client.CustomObjectsApi",
        return_value=_mock_troshka_ceph_osd_count(3),
    ):
        devices = discover_ceph_device_pvcs(core_api, namespace)

    assert [d["kind"] for d in devices] == ["ceph-mon", "ceph-osd", "ceph-osd", "ceph-osd"]
    assert [d["index"] for d in devices] == [0, 0, 1, 2]
    assert all(d["size_bytes"] == 100 * 1073741824 for d in devices[1:])


def test_discover_ceph_device_pvcs_fails_on_osd_count_mismatch():
    namespace = "troshka-abc"
    mon = _mock_pvc("rook-ceph-mon-a", "10Gi")
    osd = _mock_pvc(
        "osd-set-data-06p6rg",
        "150Gi",
        labels={
            "ceph.rook.io/DeviceSet": "osd-set",
            "ceph.rook.io/DeviceSetPVCId": "osd-set-data-0",
            "ceph.rook.io/setIndex": "0",
        },
    )
    core_api = MagicMock()
    core_api.read_namespaced_persistent_volume_claim.return_value = mon
    core_api.list_namespaced_persistent_volume_claim.return_value = MagicMock(items=[osd])

    with patch(
        "helpers.rook_ceph.client.CustomObjectsApi",
        return_value=_mock_troshka_ceph_osd_count(3),
    ):
        with pytest.raises(ValueError, match="expected 3 ceph-osd PVC"):
            discover_ceph_device_pvcs(core_api, namespace)


def test_discover_ceph_device_pvcs_fails_when_mon_missing():
    core_api = MagicMock()
    core_api.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)

    with patch(
        "helpers.rook_ceph.client.CustomObjectsApi",
        return_value=_mock_troshka_ceph_osd_count(1),
    ):
        with pytest.raises(ValueError, match="expected mon PVC rook-ceph-mon-a"):
            discover_ceph_device_pvcs(core_api, "troshka-abc")
