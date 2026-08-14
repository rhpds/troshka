"""Tests for pattern storage readiness placement filters."""

import uuid

from sqlalchemy import select

from app.models.host import Host
from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.project import Project
from app.models.user import User
from app.services import placement
from tests.conftest import TestSession


def _host(db, provider_id, ram=64000, vcpus=32):
    """Create a test host."""
    h = Host(
        id=str(uuid.uuid4()),
        state="active",
        agent_status="connected",
        host_type="kubevirt-cluster",
        provider_id=provider_id,
        total_vcpus=vcpus,
        total_ram_mb=ram,
        used_vcpus=0,
        used_ram_mb=0,
        max_eips=0,
        ip_address="10.0.0.1",
    )
    db.add(h)
    db.flush()
    return h


def _pattern_disk(db):
    """Create a test pattern disk."""
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
        s3_key="patterns/p/d.qcow2",
        format="qcow2",
        size_bytes=100,
        state="available",
    )
    db.add(pd)
    db.flush()
    return pd


def test_find_host_skips_unready_cluster():
    """find_available_host returns None when cluster has no synced PatternLocation."""
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        _host(db, prov)
        pd = _pattern_disk(db)
        host = placement.find_available_host(db, 4, 8000, pattern_disk_ids=[pd.id])
        assert host is None
    finally:
        db.close()


def test_find_host_allows_ready_cluster():
    """find_available_host returns the host when a central synced PatternLocation exists."""
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        h = _host(db, prov)
        pd = _pattern_disk(db)
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
        host = placement.find_available_host(db, 4, 8000, pattern_disk_ids=[pd.id])
        assert host is not None
        assert host.id == h.id
    finally:
        db.close()


def test_storage_ready_anywhere():
    """_storage_ready_anywhere returns False then True after adding a location."""
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        _host(db, prov)
        pd = _pattern_disk(db)
        assert placement._storage_ready_anywhere(db, [pd.id]) is False
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=prov,
                location_type="obc",
                s3_key=pd.s3_key,
                state="synced",
            )
        )
        db.flush()
        assert placement._storage_ready_anywhere(db, [pd.id]) is True
    finally:
        db.close()


def test_place_project_syncing_message():
    """place_project returns the syncing message when pattern_disk_ids are present but no location exists anywhere."""
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        _host(db, prov)
        pd = _pattern_disk(db)
        user = db.scalars(select(User)).first()
        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
                {
                    "id": "s1",
                    "type": "storageNode",
                    "data": {
                        "source": "pattern",
                        "patternId": pd.pattern_id,
                        "patternDiskId": pd.id,
                    },
                },
            ]
        }
        proj = Project(
            id=str(uuid.uuid4()),
            name="proj",
            owner_id=user.id,
            provider_id=prov,
            topology=topo,
            state="draft",
        )
        db.add(proj)
        db.flush()
        result = placement.place_project(db, proj)
        assert "syncing" in result.get("error", "").lower()
    finally:
        db.close()
