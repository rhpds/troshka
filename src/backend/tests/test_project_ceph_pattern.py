"""Tests for project Ceph pattern capture topology helpers."""

import pytest

from app.services.project_ceph_pattern import (
    CEPH_SOURCE_MON,
    CEPH_SOURCE_OSD,
    get_project_ceph_capture,
    set_ceph_restore_devices,
    set_project_ceph_capture,
    strip_runtime_project_ceph,
    validate_ceph_capture_disk_set,
)


def test_ceph_source_kind_constants():
    assert CEPH_SOURCE_MON == "ceph-mon"
    assert CEPH_SOURCE_OSD == "ceph-osd"


def test_strip_runtime_project_ceph():
    topo = {"projectCeph": {"fsid": "x"}, "nodes": []}
    strip_runtime_project_ceph(topo)
    assert "projectCeph" not in topo


def test_set_and_get_project_ceph_capture():
    topo = {"nodes": []}
    set_project_ceph_capture(topo, mon_disk_id="m1", osd_disk_ids=["o0", "o1"])
    assert get_project_ceph_capture(topo) == {
        "monDiskId": "m1",
        "osdDiskIds": ["o0", "o1"],
    }


def test_get_project_ceph_capture_missing():
    assert get_project_ceph_capture({"nodes": []}) is None


def test_validate_ceph_capture_disk_set_rejects_partial():
    with pytest.raises(ValueError, match="osd"):
        validate_ceph_capture_disk_set(3, [{"kind": "ceph-mon"}, {"kind": "ceph-osd"}])


def test_set_ceph_restore_devices_stamps_restore_block():
    topo = {"nodes": []}
    set_project_ceph_capture(topo, mon_disk_id="m1", osd_disk_ids=["o0", "o1"])
    mon = {"s3Path": "patterns/p/ceph-mon-0.tar.gz", "source": "obc"}
    osds = [
        {"index": 0, "s3Path": "patterns/p/ceph-osd-0.qcow2", "source": "obc"},
        {"index": 1, "s3Path": "patterns/p/ceph-osd-1.qcow2", "source": "central"},
    ]
    set_ceph_restore_devices(topo, mon=mon, osds=osds)

    assert topo["projectCephCapture"]["restore"] == {"mon": mon, "osds": osds}
    # Underlying disk ids stay intact for downstream consumers.
    assert get_project_ceph_capture(topo) == {
        "monDiskId": "m1",
        "osdDiskIds": ["o0", "o1"],
    }


def test_set_ceph_restore_devices_noop_without_capture():
    topo = {"nodes": []}
    set_ceph_restore_devices(topo, mon={"s3Path": "x"}, osds=[])
    assert "projectCephCapture" not in topo
