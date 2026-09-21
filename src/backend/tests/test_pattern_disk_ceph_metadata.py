import uuid

import pytest

from app.models.pattern import Pattern, PatternDisk
from app.models.user import User
from tests.conftest import TestSession


@pytest.fixture
def db_session():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


def test_pattern_disk_accepts_ceph_metadata(db_session):
    user = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.com",
        display_name="Test User",
        role="user",
    )
    db_session.add(user)
    db_session.flush()

    pattern = Pattern(
        id=str(uuid.uuid4()),
        name="ceph-pattern",
        owner_id=user.id,
        topology={"nodes": [], "edges": []},
    )
    db_session.add(pattern)
    db_session.flush()

    disk = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pattern.id,
        source_disk_id="ceph-osd-0",
        source_vm_id=None,
        source_kind="ceph-osd",
        source_index=0,
        source_pvc_name="osd-set-data-0-xxxxx",
        s3_key="patterns/ceph/osd-0.qcow2",
        format="qcow2",
        state="available",
    )
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)

    assert disk.source_kind == "ceph-osd"
    assert disk.source_index == 0
    assert disk.source_pvc_name == "osd-set-data-0-xxxxx"
    assert disk.source_vm_id is None
