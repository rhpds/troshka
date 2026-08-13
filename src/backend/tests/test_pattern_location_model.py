import uuid

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from tests.conftest import TestSession


def _make_pattern_disk(db):
    user = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.com",
        display_name="Test User",
        role="user",
    )
    db.add(user)
    db.flush()
    pattern = Pattern(
        id=str(uuid.uuid4()),
        name="p",
        owner_id=user.id,
        topology={"nodes": []},
    )
    db.add(pattern)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pattern.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key="patterns/x/d.qcow2",
        format="qcow2",
        state="available",
    )
    db.add(pd)
    db.flush()
    return pd


def test_obc_location_defaults():
    db = TestSession()
    pd = _make_pattern_disk(db)
    loc = PatternLocation(
        pattern_disk_id=pd.id,
        provider_id=str(uuid.uuid4()),
        s3_key=pd.s3_key,
        state="synced",
    )
    db.add(loc)
    db.flush()
    assert loc.location_type == "obc"
    db.close()


def test_central_location_has_null_provider():
    db = TestSession()
    pd = _make_pattern_disk(db)
    loc = PatternLocation(
        pattern_disk_id=pd.id,
        provider_id=None,
        location_type="central",
        s3_key=pd.s3_key,
        state="synced",
    )
    db.add(loc)
    db.flush()
    assert loc.provider_id is None
    assert loc.location_type == "central"
    db.close()
