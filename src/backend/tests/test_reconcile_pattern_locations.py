"""Tests for reconcile_pattern_locations backfill script."""

import uuid

import pytest
from sqlalchemy import delete

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.scripts.reconcile_pattern_locations import reconcile_pattern_locations
from tests.conftest import TestSession


@pytest.fixture(autouse=True)
def _isolate_pattern_tables():
    def _clean():
        db = TestSession()
        try:
            db.execute(delete(PatternLocation))
            db.execute(delete(PatternDisk))
            db.execute(delete(Pattern))
            db.commit()
        finally:
            db.close()

    _clean()
    yield
    _clean()


def _disk(db, key):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", display_name="t")
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
    return pd


def _loc(db, pd, location_type):
    row = PatternLocation(
        pattern_disk_id=pd.id,
        provider_id=None,
        location_type=location_type,
        s3_key=pd.s3_key,
        state="synced",
    )
    db.add(row)
    db.flush()
    return row


def test_object_in_central_stays_central():
    db = TestSession()
    try:
        pd = _disk(db, "patterns/p/d.qcow2")
        row = _loc(db, pd, "central")
        summary = reconcile_pattern_locations(
            db,
            exists_in_central=lambda k: True,
            exists_in_gold=lambda k: False,
        )
        db.refresh(row)
        assert row.location_type == "central"
        assert row.state == "synced"
        assert summary["central"] == 1
    finally:
        db.close()


def test_central_labeled_object_only_in_gold_is_reclassified():
    # A row the old central_library scan mislabeled "central" but whose object
    # lives in the read-only gold store must be reclassified to "gold".
    db = TestSession()
    try:
        pd = _disk(db, "patterns/p/d.qcow2")
        row = _loc(db, pd, "central")
        summary = reconcile_pattern_locations(
            db,
            exists_in_central=lambda k: False,
            exists_in_gold=lambda k: True,
        )
        db.refresh(row)
        assert row.location_type == "gold"
        assert row.state == "synced"
        assert summary["gold"] == 1
    finally:
        db.close()


def test_object_nowhere_marks_error():
    db = TestSession()
    try:
        pd = _disk(db, "patterns/p/d.qcow2")
        row = _loc(db, pd, "central")
        summary = reconcile_pattern_locations(
            db,
            exists_in_central=lambda k: False,
            exists_in_gold=lambda k: False,
        )
        db.refresh(row)
        assert row.state == "error"
        assert summary["error"] == 1
    finally:
        db.close()


def test_obc_rows_untouched():
    db = TestSession()
    try:
        pd = _disk(db, "patterns/p/d.qcow2")
        obc = PatternLocation(
            pattern_disk_id=pd.id,
            provider_id=str(uuid.uuid4()),
            location_type="obc",
            s3_key=pd.s3_key,
            state="synced",
        )
        db.add(obc)
        db.flush()
        reconcile_pattern_locations(
            db,
            exists_in_central=lambda k: False,
            exists_in_gold=lambda k: False,
        )
        db.refresh(obc)
        assert obc.location_type == "obc"
        assert obc.state == "synced"
    finally:
        db.close()
