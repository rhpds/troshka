"""Tests for backfill_pattern_locations script."""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.core.database import Base
from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.scripts.backfill_pattern_locations import backfill_central_locations
from tests.conftest import TestSession, test_engine


@pytest.fixture(autouse=True)
def _clear_db():
    """Clear database before each test."""
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield


def _pattern(db, key):
    """Create a test pattern with a disk."""
    user = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@e.com",
        display_name="test",
    )
    db.add(user)
    db.flush()
    pat = Pattern(id=str(uuid.uuid4()), name="p", owner_id=user.id, topology={})
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key=key,
        format="qcow2",
        size_bytes=100,
        state="available",
    )
    db.add(pd)
    db.flush()
    return pat, pd


def test_present_disk_gets_central_row():
    """When head_object succeeds, create a synced central location."""
    db = TestSession()
    try:
        pat, pd = _pattern(db, "patterns/legacy/d.qcow2")
        s3 = MagicMock()  # head_object succeeds (no exception)
        created, need_sync = backfill_central_locations(db, s3, "troshka-images")
        assert created == 1
        assert need_sync == []
        row = db.scalars(
            select(PatternLocation).filter_by(
                pattern_disk_id=pd.id, location_type="central"
            )
        ).first()
        assert row is not None
        assert row.state == "synced"
        assert row.provider_id is None
    finally:
        db.close()


def test_missing_disk_flags_sync():
    """When head_object fails (404), append pattern_id to need_sync."""
    db = TestSession()
    try:
        pat, pd = _pattern(db, "patterns/osac3/d.qcow2")
        s3 = MagicMock()
        s3.head_object.side_effect = Exception("404")
        created, need_sync = backfill_central_locations(db, s3, "troshka-images")
        assert created == 0
        assert pat.id in need_sync
    finally:
        db.close()


def test_existing_synced_central_is_skipped():
    """Skip disks that already have a synced central location."""
    db = TestSession()
    try:
        pat, pd = _pattern(db, "patterns/legacy/d.qcow2")
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=None,
                location_type="central",
                s3_key=pd.s3_key,
                state="synced",
            )
        )
        db.commit()
        s3 = MagicMock()
        created, need_sync = backfill_central_locations(db, s3, "troshka-images")
        assert created == 0
        assert need_sync == []
        s3.head_object.assert_not_called()
    finally:
        db.close()
