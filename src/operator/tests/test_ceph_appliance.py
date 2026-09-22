"""Tests for Troshka Ceph appliance (non-Rook) manifests."""

from helpers.ceph_appliance import (
    CEPH_EXTERNAL_SECRET,
    MON_NAME,
    MON_PVC_NAME,
    build_conf_configmap,
    build_export_job,
    build_external_secret,
    build_mon_deployment,
    build_mon_pvc,
    build_osd_deployment,
    build_osd_pvcs,
    lab_cidr,
    osd_lab_ip,
    osd_pvc_name,
)


def _cr(**spec_extra):
    return {
        "kind": "TroshkaCeph",
        "metadata": {
            "namespace": "troshka-abc",
            "name": "project-ceph",
            "uid": "uid-1",
        },
        "spec": {
            "labIp": "10.0.0.4",
            "labPrefixLength": 24,
            "networkNad": "net-abcd1234-nad",
            "capacityGi": 150,
            "osdCount": 1,
            **spec_extra,
        },
    }


def test_osd_lab_ip_offsets():
    assert osd_lab_ip("10.0.0.4", 0) == "10.0.0.20"
    assert osd_lab_ip("10.0.0.4", 2) == "10.0.0.22"


def test_lab_cidr():
    assert lab_cidr("10.0.0.4", "24") == "10.0.0.0/24"


def test_build_mon_pvc_and_osd_pvcs():
    cr = _cr()
    mon = build_mon_pvc(cr)
    assert mon["metadata"]["name"] == MON_PVC_NAME
    assert mon["spec"]["accessModes"] == ["ReadWriteOnce"]
    osds = build_osd_pvcs(cr)
    assert len(osds) == 1
    assert osds[0]["metadata"]["name"] == osd_pvc_name(0)
    assert osds[0]["spec"]["volumeMode"] == "Block"
    assert osds[0]["metadata"]["labels"]["troshka-ceph-osd-index"] == "0"


def test_build_osd_pvcs_skipped_in_restore():
    cr = _cr(
        restore={
            "enabled": True,
            "monPvc": MON_PVC_NAME,
            "osdPvcs": ["troshka-ceph-osd-0"],
        }
    )
    assert build_osd_pvcs(cr) == []


def test_mon_deployment_multus_and_lab_ip():
    cr = _cr()
    dep = build_mon_deployment(cr, "quay.io/ceph/ceph:v19")
    assert dep["metadata"]["name"] == MON_NAME
    pod = dep["spec"]["template"]
    assert (
        pod["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"]
        == "net-abcd1234-nad"
    )
    env = {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}
    assert env["LAB_IP"] == "10.0.0.4"
    assert any(c["name"] == "mon" for c in pod["spec"]["containers"])
    assert any(c["name"] == "mgr" for c in pod["spec"]["containers"])


def test_osd_deployment_uses_offset_ip():
    cr = _cr()
    dep = build_osd_deployment(cr, "quay.io/ceph/ceph:v19", 0)
    env = {
        e["name"]: e.get("value")
        for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["OSD_IP"] == "10.0.0.20"
    assert dep["metadata"]["name"] == "troshka-ceph-osd-0"


def test_external_secret_mon_host_is_lab_ip():
    secret = build_external_secret(_cr(), fsid="fsid-1")
    assert secret["metadata"]["name"] == CEPH_EXTERNAL_SECRET
    assert secret["stringData"]["mon-host"] == "10.0.0.4:3300"
    assert secret["stringData"]["fsid"] == "fsid-1"


def test_conf_configmap_single_lab_mon_host():
    cm = build_conf_configmap(_cr(), fsid="fsid-1")
    conf = cm["data"]["ceph.conf"]
    assert "mon host = [v2:10.0.0.4:3300/0]" in conf
    assert "public addr = 10.0.0.4:3300" in conf
    assert "fsid = fsid-1" in conf


def test_export_job_targets_appliance_mon_label():
    job = build_export_job(_cr())
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "app=troshka-ceph-mon" in script
    assert "10.0.0.4" not in script or "lab_ip" in script
    assert "external_cluster_details" in script
