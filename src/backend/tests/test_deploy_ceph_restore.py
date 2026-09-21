"""Tests for resolving projectCephCapture mon/OSD PatternDisk ids into S3
restore devices at deploy time (Task 9) — the operator has no DB access, so
this must happen in the backend before the TroshkaProject CR is created.
"""

import uuid
from unittest.mock import MagicMock

import pytest

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.services import deploy_service
from app.services.project_ceph_pattern import (
    get_project_ceph_capture,
    set_project_ceph_capture,
)
from tests.conftest import TestSession

PROV = str(uuid.uuid4())


def _make_db():
    return TestSession()


def _ceph_disk(db, pat, *, kind, index, s3_key, size_bytes=1000, virtual_bytes=2000):
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id=f"ceph-{kind}-{index}",
        source_vm_id=None,
        source_kind=kind,
        source_index=index,
        source_pvc_name=f"pvc-{kind}-{index}",
        s3_key=s3_key,
        format="tar.gz" if kind == "ceph-mon" else "qcow2",
        size_bytes=size_bytes,
        virtual_size_bytes=virtual_bytes,
        state="available",
    )
    db.add(pd)
    db.flush()
    return pd


def _synced(db, disk_id, location_type, provider_id=None):
    db.add(
        PatternLocation(
            pattern_disk_id=disk_id,
            provider_id=provider_id,
            location_type=location_type,
            s3_key="x",
            state="synced",
        )
    )
    db.flush()


def _pattern(db):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", display_name="t")
    db.add(user)
    db.flush()
    pat = Pattern(id=str(uuid.uuid4()), name="p", owner_id=user.id, topology={})
    db.add(pat)
    db.flush()
    return pat


class TestResolveCephCaptureS3Paths:
    def test_noop_when_no_capture(self):
        topology = {"nodes": []}
        db = _make_db()
        try:
            deploy_service._resolve_ceph_capture_s3_paths(topology, db, PROV)
            assert "projectCephCapture" not in topology
        finally:
            db.rollback()
            db.close()

    def test_resolves_mon_and_osd_devices_onto_topology(self):
        db = _make_db()
        try:
            pat = _pattern(db)
            mon = _ceph_disk(
                db, pat, kind="ceph-mon", index=0, s3_key="patterns/p/ceph-mon-0.tar.gz"
            )
            osd0 = _ceph_disk(
                db, pat, kind="ceph-osd", index=0, s3_key="patterns/p/ceph-osd-0.qcow2"
            )
            osd1 = _ceph_disk(
                db, pat, kind="ceph-osd", index=1, s3_key="patterns/p/ceph-osd-1.qcow2"
            )
            _synced(db, mon.id, "obc", PROV)
            _synced(db, osd0.id, "obc", PROV)
            _synced(db, osd1.id, "central", None)

            topology = {}
            set_project_ceph_capture(
                topology, mon_disk_id=mon.id, osd_disk_ids=[osd0.id, osd1.id]
            )

            deploy_service._resolve_ceph_capture_s3_paths(topology, db, PROV)

            restore = topology["projectCephCapture"]["restore"]
            assert restore["mon"]["s3Path"] == "patterns/p/ceph-mon-0.tar.gz"
            assert restore["mon"]["source"] == "obc"
            assert restore["mon"]["virtualSizeBytes"] == 2000
            assert len(restore["osds"]) == 2
            by_index = {d["index"]: d for d in restore["osds"]}
            assert by_index[0]["s3Path"] == "patterns/p/ceph-osd-0.qcow2"
            assert by_index[0]["source"] == "obc"
            assert by_index[1]["s3Path"] == "patterns/p/ceph-osd-1.qcow2"
            assert by_index[1]["source"] == "central"

            # get_project_ceph_capture keeps returning the original disk ids
            # unchanged — the restore block is additive, not a replacement.
            capture = get_project_ceph_capture(topology)
            assert capture["monDiskId"] == mon.id
            assert capture["osdDiskIds"] == [osd0.id, osd1.id]
        finally:
            db.rollback()
            db.close()

    def test_raises_when_mon_disk_unreachable(self):
        db = _make_db()
        try:
            pat = _pattern(db)
            mon = _ceph_disk(
                db, pat, kind="ceph-mon", index=0, s3_key="patterns/p/ceph-mon-0.tar.gz"
            )
            osd0 = _ceph_disk(
                db, pat, kind="ceph-osd", index=0, s3_key="patterns/p/ceph-osd-0.qcow2"
            )
            _synced(db, osd0.id, "central", None)
            # mon has no PatternLocation anywhere -> not reachable.

            topology = {}
            set_project_ceph_capture(
                topology, mon_disk_id=mon.id, osd_disk_ids=[osd0.id]
            )

            with pytest.raises(deploy_service.DeployError):
                deploy_service._resolve_ceph_capture_s3_paths(topology, db, PROV)
        finally:
            db.rollback()
            db.close()


class TestPreflightVerifyCephCapture:
    def test_skips_when_no_capture(self):
        s3_client = MagicMock()
        deploy_service._preflight_verify_ceph_capture({"nodes": []}, s3_client, "b", {})
        s3_client.head_object.assert_not_called()

    def test_skips_obc_devices(self):
        s3_client = MagicMock()
        topology = {}
        set_project_ceph_capture(topology, mon_disk_id="d1", osd_disk_ids=["d2"])
        topology["projectCephCapture"]["restore"] = {
            "mon": {"s3Path": "patterns/p/ceph-mon-0.tar.gz", "source": "obc"},
            "osds": [
                {"index": 0, "s3Path": "patterns/p/ceph-osd-0.qcow2", "source": "obc"}
            ],
        }
        deploy_service._preflight_verify_ceph_capture(topology, s3_client, "b", {})
        s3_client.head_object.assert_not_called()

    def test_raises_on_missing_central_device(self):
        s3_client = MagicMock()
        s3_client.head_object.side_effect = Exception("404 NoSuchKey")
        topology = {}
        set_project_ceph_capture(topology, mon_disk_id="d1", osd_disk_ids=["d2"])
        topology["projectCephCapture"]["restore"] = {
            "mon": {"s3Path": "patterns/p/ceph-mon-0.tar.gz", "source": "central"},
            "osds": [],
        }
        with pytest.raises(deploy_service.DeployError):
            deploy_service._preflight_verify_ceph_capture(topology, s3_client, "b", {})

    def test_skips_when_no_central_client(self):
        topology = {}
        set_project_ceph_capture(topology, mon_disk_id="d1", osd_disk_ids=[])
        topology["projectCephCapture"]["restore"] = {
            "mon": {"s3Path": "patterns/p/ceph-mon-0.tar.gz", "source": "central"},
            "osds": [],
        }
        # No exception when s3_client itself is falsy (central S4 not configured).
        deploy_service._preflight_verify_ceph_capture(topology, None, "b", {})
