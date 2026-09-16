"""Tests for Project Ceph rook manifest helpers."""

from unittest.mock import MagicMock

from helpers.rook_ceph import (
    TROSHKA_ROOK_SA_CLUSTER_ROLES,
    build_ceph_cluster,
    build_rook_operator_config,
    build_rook_operator_deployment,
    data_dir_host_path,
    default_lab_ip_from_cidr,
    discover_ceph_image,
    normalize_ceph_counts,
    rook_crb_name,
    validate_lab_ip,
)


def test_default_lab_ip_from_cidr():
    assert default_lab_ip_from_cidr("10.0.0.0/24") == "10.0.0.3"
    assert default_lab_ip_from_cidr("") == ""


def test_normalize_ceph_counts_clamps():
    osd, repl, per = normalize_ceph_counts({"osdCount": 9, "capacityGi": 100})
    assert osd == 6
    assert repl == 3
    assert per >= 50


def test_build_ceph_cluster_omits_removed_rook_fields():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {"namespace": "troshka-abc", "name": "project-ceph", "uid": "uid-1"},
        "spec": {"labIp": "10.0.0.3", "capacityGi": 300, "osdCount": 3},
    }
    cluster = build_ceph_cluster(cr)
    assert "removeOSDsIfOutOfSafeRange" not in cluster["spec"]
    assert "cleanupPolicy" not in cluster["spec"]


def test_build_ceph_cluster_mons_use_pvcs_not_shared_odf_path():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {"namespace": "troshka-abc", "name": "project-ceph", "uid": "uid-1"},
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
        "metadata": {"namespace": "troshka-abc", "name": "project-ceph", "uid": "uid-1"},
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
        "metadata": {"namespace": "troshka-abc", "name": "project-ceph", "uid": "uid-1"},
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
        "metadata": {"namespace": "troshka-abc", "name": "project-ceph", "uid": "uid-1"},
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
        "metadata": {"namespace": "troshka-abc", "name": "project-ceph", "uid": "uid-1"},
        "spec": {"labIp": "10.0.0.3"},
    }
    data = build_rook_operator_config(cr)["data"]
    assert data["ROOK_HOSTPATH_REQUIRES_PRIVILEGED"] == "true"
    assert data["ROOK_CEPH_MON_RUN_AS_ROOT"] == "true"


def test_discover_ceph_image_from_odf():
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {
        "items": [{"spec": {"cephVersion": {"image": "registry.example/ceph@sha256:abc"}}}]
    }
    assert discover_ceph_image(api) == "registry.example/ceph@sha256:abc"


def test_build_ceph_cluster_uses_discovered_image():
    cr = {
        "kind": "TroshkaCeph",
        "metadata": {"namespace": "troshka-abc", "name": "project-ceph", "uid": "uid-1"},
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
        "metadata": {"namespace": "troshka-abc", "name": "project-ceph", "uid": "uid-1"},
        "spec": {"labIp": "10.0.0.3"},
    }
    env_names = {
        e["name"]
        for e in build_rook_operator_deployment(cr)["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]
    }
    assert {"POD_NAME", "POD_NAMESPACE", "NODE_NAME"} <= env_names
