"""Tests for Project Ceph rook manifest helpers."""

from helpers.rook_ceph import (
    build_ceph_cluster,
    default_lab_ip_from_cidr,
    normalize_ceph_counts,
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
