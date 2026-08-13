import uuid

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.services.pattern_locations import (
    pattern_disk_ids_from_topology,
    pattern_disk_source_for_cluster,
    pattern_disks_ready_on_provider,
)
from tests.conftest import TestSession


def _disk(db):
    user = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.com",
        display_name="Test User",
    )
    db.add(user)
    db.flush()
    pat = Pattern(
        id=str(uuid.uuid4()),
        name="p",
        owner_id=user.id,
        topology={"nodes": []},
    )
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key="patterns/x/d.qcow2",
        format="qcow2",
        state="available",
    )
    db.add(pd)
    db.flush()
    return pd


PROV_A = str(uuid.uuid4())
PROV_B = str(uuid.uuid4())


def test_ids_from_topology():
    topo = {
        "nodes": [
            {
                "type": "storageNode",
                "data": {"source": "pattern", "patternDiskId": "d1"},
            },
            {
                "type": "storageNode",
                "data": {"source": "library", "libraryItemId": "l1"},
            },
            {"type": "vmNode", "data": {}},
        ]
    }
    assert pattern_disk_ids_from_topology(topo) == ["d1"]


def test_obc_on_target_provider():
    db = TestSession()
    try:
        pd = _disk(db)
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=PROV_A,
                location_type="obc",
                s3_key=pd.s3_key,
                state="synced",
            )
        )
        db.flush()
        assert pattern_disk_source_for_cluster(db, pd.id, PROV_A) == "obc"
        # OBC on A is not visible to B, and no central row → None on B
        assert pattern_disk_source_for_cluster(db, pd.id, PROV_B) is None
    finally:
        db.close()


def test_central_visible_everywhere():
    db = TestSession()
    try:
        pd = _disk(db)
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=None,
                location_type="central",
                s3_key=pd.s3_key,
                state="synced",
            )
        )
        db.flush()
        assert pattern_disk_source_for_cluster(db, pd.id, PROV_A) == "central"
        assert pattern_disk_source_for_cluster(db, pd.id, PROV_B) == "central"
    finally:
        db.close()


def test_obc_preferred_over_central_on_source():
    db = TestSession()
    try:
        pd = _disk(db)
        db.add_all(
            [
                PatternLocation(
                    pattern_disk_id=pd.id,
                    provider_id=PROV_A,
                    location_type="obc",
                    s3_key=pd.s3_key,
                    state="synced",
                ),
                PatternLocation(
                    pattern_disk_id=pd.id,
                    provider_id=None,
                    location_type="central",
                    s3_key=pd.s3_key,
                    state="synced",
                ),
            ]
        )
        db.flush()
        assert pattern_disk_source_for_cluster(db, pd.id, PROV_A) == "obc"
    finally:
        db.close()


def test_syncing_central_is_not_ready():
    db = TestSession()
    try:
        pd = _disk(db)
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=None,
                location_type="central",
                s3_key=pd.s3_key,
                state="syncing",
            )
        )
        db.flush()
        assert pattern_disk_source_for_cluster(db, pd.id, PROV_A) is None
    finally:
        db.close()


def test_ready_requires_all_disks():
    db = TestSession()
    try:
        pd1 = _disk(db)
        pd2 = _disk(db)
        db.add(
            PatternLocation(
                pattern_disk_id=pd1.id,
                provider_id=None,
                location_type="central",
                s3_key=pd1.s3_key,
                state="synced",
            )
        )
        db.flush()
        assert pattern_disks_ready_on_provider(db, [pd1.id], PROV_A) is True
        assert pattern_disks_ready_on_provider(db, [pd1.id, pd2.id], PROV_A) is False
        assert pattern_disks_ready_on_provider(db, [], PROV_A) is True
    finally:
        db.close()
