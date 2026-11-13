"""Explicit DB preparation repairs create_all's omitted migration seeds only."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.core import database
from app.core.database import Base
from app.models.library import Library, LibraryItem
from app.models.managed_group import ManagedGroup
from app.models.metering import MeteringInterval
from app.models.pattern import Pattern
from app.models.project import Project
from app.models.quota import QuotaPolicy, QuotaReadiness
from app.models.user import User
from app.services import database_access

GROUP_ID = "dc591874-9d97-4bf5-ab31-581bb95cc645"
GROUP_NORMALIZED_NAME = "quota-default-dc591874-9d97-4bf5-ab31-581bb95cc645"
POLICY_ID = "cc4ecf45-0811-48a5-bca2-e440a3fbc55c"


@pytest.fixture(params=["sqlite", "postgresql"])
def preparation(request, tmp_path, monkeypatch):
    """All real Alembic commands and application writes use a disposable DB."""
    schema = None
    if request.param == "postgresql":
        url = os.getenv("QUOTA_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("disposable PostgreSQL URL not configured")
        engine = sa.create_engine(url)
        schema = "quota_foundation_" + uuid4().hex
        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

        @sa.event.listens_for(engine, "checkout")
        def search_path(connection, _record, _proxy):
            cursor = connection.cursor()
            cursor.execute(f'SET search_path TO "{schema}"')
            connection.commit()
            cursor.close()

    else:
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'foundation.db'}")

    monkeypatch.setattr(database, "engine", engine)
    # env.py owns its engine factory; keep its real stamp/upgrade in this DB.
    monkeypatch.setattr(sa, "engine_from_config", lambda *args, **kwargs: engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database_access, "SessionLocal", sessions)
    try:
        yield engine, sessions
    finally:
        if schema:
            with engine.begin() as connection:
                connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def _migration_config():
    from pathlib import Path

    root = Path(__file__).parents[1]
    result = Config(str(root / "alembic.ini"))
    result.set_main_option("script_location", str(root / "alembic"))
    return result


def _stamped_without_seeds(engine):
    Base.metadata.create_all(engine)
    command.stamp(_migration_config(), "head")


def _snapshot(engine):
    with engine.connect() as connection:
        return {
            name: sorted(
                (dict(row) for row in connection.execute(sa.select(table)).mappings()),
                key=lambda row: str(row),
            )
            for name, table in Base.metadata.tables.items()
        }


def _assert_foundation(sessions):
    with sessions() as db:
        group = db.get(ManagedGroup, GROUP_ID)
        assert group is not None, "create_all/stamp omitted the foundation group"
        assert group.normalized_name == GROUP_NORMALIZED_NAME
        assert group.name == "Default organization"
        assert (
            group.description
            == "Migration attribution; not evidence of historical employer"
        )
        assert group.is_organization is True
        policy = db.get(QuotaPolicy, POLICY_ID)
        assert policy is not None
        assert (policy.scope, policy.scope_id, policy.revision) == (
            "organization",
            GROUP_ID,
            1,
        )
        assert all(
            getattr(policy, field) is None
            for field in (
                "cpu",
                "ram_bytes",
                "storage_bytes",
                "projects",
                "spending_usd",
            )
        )
        baseline = db.get(QuotaReadiness, "baseline")
        assert baseline is not None
        assert baseline.ready is False and baseline.details == {}
        assert baseline.checked_at is None and baseline.baseline_version is None
        assert db.get(QuotaReadiness, "unified-groups-v1") is None


def test_prepare_fresh_create_all_stamps_and_seeds_foundation(preparation):
    engine, sessions = preparation
    database_access.prepare_database()
    _assert_foundation(sessions)
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == ScriptDirectory.from_config(_migration_config()).get_current_head()
        )
    before = _snapshot(engine)
    database_access.prepare_database()
    assert _snapshot(engine) == before


def test_prepare_already_stamped_repairs_only_missing_seeds(preparation):
    engine, sessions = preparation
    _stamped_without_seeds(engine)
    now = datetime(2026, 11, 13, tzinfo=UTC)
    with sessions.begin() as db:
        assigned_group = ManagedGroup(name="Assigned", normalized_name="assigned")
        db.add(assigned_group)
        db.flush()
        db.add(User(email="assigned@test.invalid", organization_id=assigned_group.id))
        owner = User(email="owner@test.invalid", role="admin", access_enabled=False)
        db.add(owner)
        db.flush()
        project = Project(
            name="preserved draft",
            owner_id=owner.id,
            state="draft",
            topology={"nodes": [{"id": "kept"}]},
        )
        library = Library(type="personal", owner_id=owner.id)
        db.add_all([project, library])
        db.flush()
        db.add_all(
            [
                LibraryItem(
                    library_id=library.id,
                    name="preserved image",
                    type="image",
                    format="qcow2",
                    s3_key="preserved/image.qcow2",
                ),
                Pattern(
                    name="preserved pattern", owner_id=owner.id, topology={"nodes": []}
                ),
                MeteringInterval(
                    project_id=project.id,
                    kind="cpu",
                    resource_id="kept",
                    qty=2,
                    unit_rate=1,
                    started_at=now,
                ),
            ]
        )
    before = _snapshot(engine)
    database_access.prepare_database()
    _assert_foundation(sessions)
    after = _snapshot(engine)
    for name in before.keys() - {"managed_groups", "quota_policies", "quota_readiness"}:
        assert after[name] == before[name], name
    with sessions() as db:
        assert (
            db.query(User).filter_by(email="owner@test.invalid").one().organization_id
            is None
        )
    database_access.prepare_database()
    assert _snapshot(engine) == after


@pytest.mark.parametrize("missing", ["group", "policy", "baseline", None])
def test_prepare_preserves_existing_policy_readiness_and_partial_seeds(
    preparation, missing
):
    engine, sessions = preparation
    _stamped_without_seeds(engine)
    with sessions.begin() as db:
        if missing != "group":
            db.add(
                ManagedGroup(
                    id=GROUP_ID,
                    name="Default organization",
                    normalized_name=GROUP_NORMALIZED_NAME,
                    is_organization=True,
                    description="operator note",
                )
            )
        if missing != "policy":
            db.add(
                QuotaPolicy(
                    id=POLICY_ID,
                    scope="organization",
                    scope_id=GROUP_ID,
                    cpu=7,
                    spending_usd=12,
                    revision=8,
                )
            )
        if missing != "baseline":
            db.add(
                QuotaReadiness(
                    id="baseline",
                    ready=True,
                    details={"accounting_ready": True},
                    checked_at=datetime(2026, 11, 12, tzinfo=UTC),
                    baseline_version="kept",
                )
            )
        db.add(
            QuotaReadiness(
                id="unified-groups-v1", ready=False, details={"receipt": "kept"}
            )
        )
    before = _snapshot(engine)
    database_access.prepare_database()
    after = _snapshot(engine)
    for name in before:
        for row in before[name]:
            assert row in after[name], name
    assert len(after["managed_groups"]) == len(after["quota_policies"]) == 1
    assert len(after["quota_readiness"]) == 2
    database_access.prepare_database()
    assert _snapshot(engine) == after


@pytest.mark.parametrize(
    "conflict", ["group_id", "group_key", "group_kind", "policy_id", "policy_scope"]
)
def test_prepare_conflicting_foundation_identity_rolls_back(preparation, conflict):
    engine, sessions = preparation
    _stamped_without_seeds(engine)
    with sessions.begin() as db:
        if conflict == "group_id":
            db.add(
                ManagedGroup(id=GROUP_ID, name="Unrelated", normalized_name="unrelated")
            )
        elif conflict == "group_key":
            db.add(
                ManagedGroup(name="Unrelated", normalized_name=GROUP_NORMALIZED_NAME)
            )
        elif conflict == "group_kind":
            db.add(
                ManagedGroup(
                    id=GROUP_ID,
                    name="Default organization",
                    normalized_name=GROUP_NORMALIZED_NAME,
                    is_organization=False,
                )
            )
        elif conflict == "policy_id":
            db.add(QuotaPolicy(id=POLICY_ID, scope="user", scope_id=str(uuid4())))
        else:
            db.add(QuotaPolicy(scope="organization", scope_id=GROUP_ID, cpu=4))
    before = _snapshot(engine)
    with pytest.raises(RuntimeError, match="[Cc]onflicting quota foundation"):
        database_access.prepare_database()
    assert _snapshot(engine) == before


def test_prepare_does_not_adopt_same_display_name(preparation):
    engine, sessions = preparation
    _stamped_without_seeds(engine)
    with sessions.begin() as db:
        db.add(
            ManagedGroup(
                name="Default organization", normalized_name="default organization"
            )
        )
    database_access.prepare_database()
    _assert_foundation(sessions)
    with sessions() as db:
        assert db.query(ManagedGroup).count() == 2
