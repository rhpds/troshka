"""Tests for project Ceph helpers."""

from app.services.project_ceph import (
    build_project_ceph_stamp,
    merge_project_ceph_extra_vars,
    topology_has_ceph,
)


def test_topology_has_ceph():
    assert not topology_has_ceph({"nodes": [{"type": "vmNode"}]})
    assert topology_has_ceph({"nodes": [{"type": "cephClusterNode"}]})


def test_build_project_ceph_stamp_defaults():
    stamp = build_project_ceph_stamp(
        namespace="troshka-abc",
        status={"phase": "Ready", "monEndpoint": "10.0.0.3:6789"},
        spec={"labIp": "10.0.0.3", "osdCount": 3},
    )
    assert stamp["phase"] == "Ready"
    assert stamp["secretNamespace"] == "troshka-abc"
    assert stamp["monHost"] == "10.0.0.3"
    assert stamp["monEndpoint"] == "10.0.0.3:6789"
    assert stamp["cclm_ceph_secret_name"] == "rook-ceph-external-cluster-details"


def test_merge_project_ceph_extra_vars():
    topo = {
        "projectCeph": {
            "troshka_ceph_secret_name": "rook-ceph-external-cluster-details",
            "troshka_ceph_mon_host": "10.0.0.3",
            "secretName": "troshka-ceph-external",
            "secretNamespace": "troshka-abc",
        }
    }
    out = merge_project_ceph_extra_vars(topo, {"foo": "bar"})
    assert out["foo"] == "bar"
    assert out["troshka_ceph_mon_host"] == "10.0.0.3"
    assert out["troshka_project_ceph_secret"] == "troshka-ceph-external"
