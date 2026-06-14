from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models import *  # noqa: F403

engine = create_engine(
    "sqlite:///./test_migration.db", connect_args={"check_same_thread": False}
)
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine)


def test_validate_migration_same_pool():
    from app.services.migration_service import validate_migration

    db = Session()

    provider = Provider(name="test-aws", type="aws")
    db.add(provider)
    db.flush()

    pool = StoragePool(
        name="test-pool", mode="shared-fsx", status="available", provider_id=provider.id
    )
    db.add(pool)
    db.flush()

    source = Host(
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        storage_pool_id=pool.id,
        total_vcpus=64,
        total_ram_mb=262144,
    )
    target = Host(
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        storage_pool_id=pool.id,
        total_vcpus=64,
        total_ram_mb=262144,
    )
    db.add_all([source, target])
    db.flush()

    user = User(
        email="admin@test.com", display_name="Admin", role="admin", auth_source="local"
    )
    db.add(user)
    db.flush()

    project = Project(
        name="test-project",
        owner_id=user.id,
        state="active",
        host_id=source.id,
        poweroff_mode="simultaneous",
    )
    db.add(project)
    db.commit()

    errors = validate_migration(db, project.id, source.id, target.id)
    assert errors == [], f"Unexpected errors: {errors}"
    db.close()


def test_validate_migration_different_pools():
    from app.services.migration_service import validate_migration

    db = Session()

    provider = db.query(Provider).first()
    pool_a = StoragePool(
        name="pool-a", mode="shared-fsx", status="available", provider_id=provider.id
    )
    pool_b = StoragePool(
        name="pool-b", mode="shared-fsx", status="available", provider_id=provider.id
    )
    db.add_all([pool_a, pool_b])
    db.flush()

    source = Host(
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        storage_pool_id=pool_a.id,
        total_vcpus=64,
        total_ram_mb=262144,
    )
    target = Host(
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        storage_pool_id=pool_b.id,
        total_vcpus=64,
        total_ram_mb=262144,
    )
    db.add_all([source, target])
    db.flush()

    user = db.query(User).first()
    project = Project(
        name="cross-pool",
        owner_id=user.id,
        state="active",
        host_id=source.id,
        poweroff_mode="simultaneous",
    )
    db.add(project)
    db.commit()

    errors = validate_migration(db, project.id, source.id, target.id)
    assert any("same storage pool" in e for e in errors)
    db.close()


def test_validate_migration_local_pool():
    from app.services.migration_service import validate_migration

    db = Session()

    provider = db.query(Provider).first()
    pool = StoragePool(
        name="local-pool", mode="local", status="available", provider_id=provider.id
    )
    db.add(pool)
    db.flush()

    source = Host(
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        storage_pool_id=pool.id,
        total_vcpus=64,
        total_ram_mb=262144,
    )
    target = Host(
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        storage_pool_id=pool.id,
        total_vcpus=64,
        total_ram_mb=262144,
    )
    db.add_all([source, target])
    db.flush()

    user = db.query(User).first()
    project = Project(
        name="local-project",
        owner_id=user.id,
        state="active",
        host_id=source.id,
        poweroff_mode="simultaneous",
    )
    db.add(project)
    db.commit()

    errors = validate_migration(db, project.id, source.id, target.id)
    assert any("shared storage" in e for e in errors)
    db.close()
