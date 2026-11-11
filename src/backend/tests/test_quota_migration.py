"""Run the additive quota migration on isolated metadata, never the app DB."""

import importlib.util
import io
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql


def migration():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/f62c48d91a07_organization_quota_foundation.py"
    )
    spec = importlib.util.spec_from_file_location("quota_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exercise_migration(connection):
    connection.execute(
        sa.text(
            "CREATE TABLE managed_groups (id UUID PRIMARY KEY, name VARCHAR(255) NOT NULL, normalized_name VARCHAR(255) UNIQUE NOT NULL, description TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)"
        )
    )
    connection.execute(
        sa.text("CREATE TABLE users (id UUID PRIMARY KEY, quota_overrides JSON)")
    )
    connection.execute(
        sa.text("CREATE TABLE managed_group_members (group_id UUID, user_id UUID)")
    )
    connection.execute(
        sa.text("CREATE TABLE projects (id UUID PRIMARY KEY, budget_usd NUMERIC(18,6))")
    )
    user_id = str(uuid.uuid4())
    for index in range(2):
        group_id = str(uuid.uuid4())
        connection.execute(
            sa.text(
                "INSERT INTO managed_groups (id,name,normalized_name,description) VALUES (:id,:name,:name,'reporting')"
            ),
            dict(id=group_id, name=f"report-{index}"),
        )
        connection.execute(
            sa.text("INSERT INTO managed_group_members VALUES (:group_id,:user_id)"),
            dict(group_id=group_id, user_id=user_id),
        )
    connection.execute(
        sa.text("INSERT INTO users VALUES (:id, :overrides)"),
        dict(id=user_id, overrides='{"legacy_cap": 3}'),
    )
    connection.execute(
        sa.text("INSERT INTO projects VALUES (:id, 12.25)"), dict(id=str(uuid.uuid4()))
    )
    module = migration()
    module.op = Operations(MigrationContext.configure(connection))
    module.upgrade()
    default_id = str(
        connection.execute(
            sa.text("SELECT id FROM managed_groups WHERE is_organization = true")
        ).scalar_one()
    )
    assert (
        str(
            connection.execute(
                sa.text("SELECT organization_id FROM users")
            ).scalar_one()
        )
        == default_id
    )
    assert (
        connection.execute(
            sa.text("SELECT COUNT(*) FROM managed_group_members")
        ).scalar()
        == 2
    )
    assert (
        connection.execute(sa.text("SELECT budget_usd FROM projects")).scalar() == 12.25
    )
    overrides = connection.execute(
        sa.text("SELECT quota_overrides FROM users")
    ).scalar()
    assert overrides == '{"legacy_cap": 3}' or overrides == {"legacy_cap": 3}
    assert (
        connection.execute(sa.text("SELECT cpu FROM quota_policies")).scalar() is None
    )
    assert not connection.execute(
        sa.text("SELECT ready FROM quota_readiness")
    ).scalar_one()
    connection.execute(
        sa.text("INSERT INTO users (id) VALUES (:id)"), dict(id=str(uuid.uuid4()))
    )
    assert (
        connection.execute(
            sa.text("SELECT COUNT(*) FROM users WHERE organization_id IS NULL")
        ).scalar()
        == 1
    )
    module.downgrade()
    assert "quota_charges" not in sa.inspect(connection).get_table_names()
    assert (
        connection.execute(sa.text("SELECT COUNT(*) FROM managed_groups")).scalar() == 2
    )


def test_quota_upgrade_and_downgrade_sqlite():
    with sa.create_engine("sqlite://").begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys=ON"))
        exercise_migration(connection)


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="isolated PostgreSQL URL not configured",
)
def test_quota_upgrade_and_downgrade_postgresql():
    engine = sa.create_engine(os.environ["QUOTA_TEST_POSTGRES_URL"])
    schema = "quota_migration_" + uuid.uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            exercise_migration(connection)
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()


def test_quota_postgresql_ddl_compiles():
    output = io.StringIO()
    module = migration()
    module.op = Operations(
        MigrationContext.configure(
            dialect=postgresql.dialect(), opts={"as_sql": True, "output_buffer": output}
        )
    )
    module.upgrade()
    module.downgrade()
    ddl = output.getvalue()
    assert "NUMERIC(38, 12)" in ddl
    assert "BIGINT" in ddl
    assert "ON DELETE RESTRICT" in ddl
