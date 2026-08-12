import uuid

import pytest

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import TestSession


@pytest.fixture
def db():
    session = TestSession()
    yield session
    # Clean up test data without dropping tables
    session.query(PatternLocation).delete()
    session.query(PatternDisk).delete()
    session.query(Pattern).filter(Pattern.name.like("test-pattern%")).delete()
    session.query(Provider).filter(Provider.name.like("test-%")).delete()
    session.query(User).filter(User.email == "test@test.com").delete()
    session.commit()
    session.close()


@pytest.fixture
def user(db):
    u = User(id=str(uuid.uuid4()), email="test@test.com", display_name="Test")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def provider(db):
    p = Provider(
        id=str(uuid.uuid4()),
        name="test-cluster",
        type="kubevirt_native",
        state="active",
    )
    db.add(p)
    db.commit()
    return p


@pytest.fixture
def pattern_with_disk(db, user):
    pattern = Pattern(
        id=str(uuid.uuid4()),
        name="test-pattern",
        owner_id=user.id,
        topology={"nodes": [], "edges": []},
        state="available",
    )
    db.add(pattern)
    db.commit()

    disk = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pattern.id,
        source_disk_id=str(uuid.uuid4()),
        source_vm_id=str(uuid.uuid4()),
        s3_key="patterns/test/disk.qcow2",
        format="qcow2",
        size_bytes=1000000,
        virtual_size_bytes=10000000,
        state="available",
    )
    db.add(disk)
    db.commit()
    return pattern, disk


class TestPatternLocationModel:
    def test_create_pattern_location(self, db, pattern_with_disk, provider):
        _, disk = pattern_with_disk
        loc = PatternLocation(
            pattern_disk_id=disk.id,
            provider_id=provider.id,
            s3_key="patterns/test/disk.qcow2",
            state="synced",
            size_bytes=1000000,
        )
        db.add(loc)
        db.commit()
        assert loc.id is not None
        assert loc.state == "synced"
        assert loc.size_bytes == 1000000

    def test_default_state_is_syncing(self, db, pattern_with_disk, provider):
        _, disk = pattern_with_disk
        loc = PatternLocation(
            pattern_disk_id=disk.id,
            provider_id=provider.id,
            s3_key="patterns/test/disk.qcow2",
        )
        db.add(loc)
        db.commit()
        assert loc.state == "syncing"

    def test_relationship_from_disk(self, db, pattern_with_disk, provider):
        _, disk = pattern_with_disk
        loc = PatternLocation(
            pattern_disk_id=disk.id,
            provider_id=provider.id,
            s3_key="patterns/test/disk.qcow2",
            state="synced",
            size_bytes=1000000,
        )
        db.add(loc)
        db.commit()
        db.refresh(disk)
        assert len(disk.locations) == 1
        assert disk.locations[0].provider_id == provider.id

    def test_cascade_delete_with_disk(self, db, pattern_with_disk, provider):
        pattern, disk = pattern_with_disk
        loc = PatternLocation(
            pattern_disk_id=disk.id,
            provider_id=provider.id,
            s3_key="patterns/test/disk.qcow2",
            state="synced",
        )
        db.add(loc)
        db.commit()
        loc_id = loc.id

        db.delete(disk)
        db.commit()

        assert db.query(PatternLocation).filter_by(id=loc_id).first() is None


class TestPatternSourceProviderId:
    def test_pattern_has_source_provider_id(self, db, user, provider):
        pattern = Pattern(
            name="test-pattern-2",
            owner_id=user.id,
            topology={"nodes": [], "edges": []},
            source_provider_id=provider.id,
        )
        db.add(pattern)
        db.commit()
        db.refresh(pattern)
        assert pattern.source_provider_id == provider.id

    def test_source_provider_id_nullable(self, db, user):
        pattern = Pattern(
            name="test-pattern-3",
            owner_id=user.id,
            topology={"nodes": [], "edges": []},
        )
        db.add(pattern)
        db.commit()
        assert pattern.source_provider_id is None
