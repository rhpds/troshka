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


def test_osd_lab_ip_fallback_is_top_down():
    # No spec.osdIps -> grab the highest-numbered addresses, not the .20+i offset
    # that collides with worker node IPs.
    assert osd_lab_ip("10.0.0.4", 0) == "10.0.0.254"
    assert osd_lab_ip("10.0.0.4", 1) == "10.0.0.253"


def test_osd_lab_ip_fallback_skips_mon_gateway_dnsmasq():
    # mon at .254 must be skipped so an OSD never lands on it.
    assert osd_lab_ip("10.0.0.254", 0) == "10.0.0.253"


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


def test_osd_deployment_fallback_top_down_when_no_spec_ips():
    cr = _cr()
    dep = build_osd_deployment(cr, "quay.io/ceph/ceph:v19", 0)
    env = {
        e["name"]: e.get("value")
        for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["OSD_IP"] == "10.0.0.254"
    assert dep["metadata"]["name"] == "troshka-ceph-osd-0"


def _osd_env_ip(cr, index):
    dep = build_osd_deployment(cr, "quay.io/ceph/ceph:v19", index)
    env = {
        e["name"]: e.get("value")
        for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    # the setup-net init container must self-assign the SAME resolved IP
    setup_cmd = dep["spec"]["template"]["spec"]["initContainers"][0]["command"][2]
    assert env["OSD_IP"] in setup_cmd
    return env["OSD_IP"]


def test_osd_deployment_prefers_spec_osd_ips():
    """Backend-allocated collision-free statics win over the legacy .20+i offset."""
    cr = _cr(osdCount=2, osdIps=["10.0.0.254", "10.0.0.253"])
    assert _osd_env_ip(cr, 0) == "10.0.0.254"
    assert _osd_env_ip(cr, 1) == "10.0.0.253"


def test_osd_deployment_falls_back_top_down_when_spec_ips_short():
    """Short osdIps falls back to top-down (collision-avoidant), never .20+i."""
    cr = _cr(osdCount=2, osdIps=["10.0.0.100"])
    assert _osd_env_ip(cr, 0) == "10.0.0.100"
    # index 1 falls back to the 1-th highest host (.253); never .21
    assert _osd_env_ip(cr, 1) == "10.0.0.253"


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


def test_osd_prepare_adopts_existing_bluestore():
    """Restored OSD block PVCs already have BlueStore — never re-create the id."""
    dep = build_osd_deployment(_cr(), "quay.io/ceph/ceph:v19", 0)
    prepare = next(
        c
        for c in dep["spec"]["template"]["spec"]["initContainers"]
        if c["name"] == "prepare"
    )
    script = prepare["command"][2]
    assert "ceph-bluestore-tool show-label" in script
    assert "bluestore present" in script
    assert "prime-osd-dir" in script
    assert 'ln -sfn "${BLOCK}" "${OSD_DIR}/block"' in script or "ln -sfn" in script
    assert "osd_uuid" in script
    # Ceph's OSD-dir "fsid" file must hold the OSD uuid, never cluster fsid.
    assert 'ceph fsid >' not in script.split("bluestore present", 1)[1].split(
        "ceph osd create", 1
    )[0]
    # Adopt must fetch the existing auth entity, not mint a new UUID / osd create.
    adopt_idx = script.index("bluestore present")
    create_idx = script.index("ceph osd create")
    assert adopt_idx < create_idx
    assert 'ceph auth get "osd.${OSD_ID}"' in script or "ceph auth get osd." in script


def test_osd_run_passes_osd_uuid_from_meta():
    """ceph-osd must start with the BlueStore label UUID, not the cluster fsid."""
    dep = build_osd_deployment(_cr(), "quay.io/ceph/ceph:v19", 0)
    run = next(
        c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "osd"
    )
    script = run["command"][2]
    assert "--osd-uuid" in script
    assert "osd_uuid" in script


def test_keyring_sync_stays_alive_on_restore_seed():
    """Sidecar must not exit 0 on restore — that CrashLoops the mon pod."""
    dep = build_mon_deployment(_cr(), "quay.io/ceph/ceph:v19")
    sync = next(
        c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "keyring-sync"
    )
    script = sync["command"][2]
    assert "seed keyrings present" in script
    # After the restore short-circuit, sleep forever (never bare exit 0).
    seed_block = script.split("seed keyrings present", 1)[1].split("sleep 2", 1)[0]
    assert "sleep infinity" in seed_block
    assert "exit 0" not in seed_block
