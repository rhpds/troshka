"""Tests for diagnose_placement_failure — the specific reason a deploy could
not be placed (CPU vs RAM vs pattern-disk availability vs no hosts)."""

import uuid

from app.models.host import Host
from app.models.pattern import Pattern, PatternDisk
from app.models.user import User
from app.services import placement
from tests.conftest import TestSession


def _host(db, provider_id, ram_mb, vcpus):
    h = Host(
        id=str(uuid.uuid4()),
        state="active",
        agent_status="connected",
        host_type="kubevirt-cluster",
        provider_id=provider_id,
        total_vcpus=vcpus,
        total_ram_mb=ram_mb,
        used_vcpus=0,
        used_ram_mb=0,
        max_eips=0,
        ip_address="10.0.0.1",
    )
    db.add(h)
    db.flush()
    return h


def _pattern_disk(db):
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
        s3_key="patterns/p/d.qcow2",
        format="qcow2",
        size_bytes=100,
        state="available",
    )
    db.add(pd)
    db.flush()
    return pd


def test_reports_no_hosts_for_empty_provider():
    db = TestSession()
    try:
        prov = str(uuid.uuid4())  # no hosts created for it
        msg = placement.diagnose_placement_failure(db, 4, 8000, provider_id=prov)
        assert "no active" in msg.lower()
    finally:
        db.close()


def test_reports_ram_when_ram_is_the_constraint():
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        _host(db, prov, ram_mb=1000, vcpus=64)  # tons of CPU, tiny RAM
        msg = placement.diagnose_placement_failure(db, 4, 200000, provider_id=prov)
        assert "ram" in msg.lower()
        assert "cpu" not in msg.lower()
    finally:
        db.close()


def test_reports_cpu_when_cpu_is_the_constraint():
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        _host(db, prov, ram_mb=256000, vcpus=1)  # tons of RAM, tiny CPU
        msg = placement.diagnose_placement_failure(db, 32, 8000, provider_id=prov)
        assert "cpu" in msg.lower()
        assert "ram" not in msg.lower()
    finally:
        db.close()


def test_reports_pattern_disk_when_storage_unavailable():
    db = TestSession()
    try:
        prov = str(uuid.uuid4())
        _host(db, prov, ram_mb=256000, vcpus=64)  # plenty of capacity
        pd = _pattern_disk(db)  # no synced location anywhere
        msg = placement.diagnose_placement_failure(
            db, 4, 8000, provider_id=prov, pattern_disk_ids=[pd.id]
        )
        assert "pattern disk" in msg.lower()
        # must NOT blame capacity — capacity is fine
        assert "not enough ram" not in msg.lower()
        assert "not enough cpu" not in msg.lower()
    finally:
        db.close()
