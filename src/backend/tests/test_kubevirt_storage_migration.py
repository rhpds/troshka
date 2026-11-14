"""Additive reporting migration, SQLite execution and PostgreSQL DDL."""

import importlib.util
import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql

MIGRATIONS = {
    "7eacbdd6fe04_native_storage_reporting_enrollment_and_.py": {
        "native_reporting_targets",
        "native_enrollment_requests",
        "native_enrollments",
        "native_collection_domains",
        "native_pool_claims",
        "native_project_roots",
        "native_retained_bindings",
        "native_storage_snapshots",
    },
    "d92b51e7046a_native_project_attempts.py": {
        "native_project_attempts",
        "native_shared_observations",
        "native_shared_storage_observations",
    },
}


def migration(filename):
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location("native_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("filename,native", MIGRATIONS.items())
def test_native_reporting_additive_migration_and_downgrade(filename, native):
    from app.core.database import Base
    from app.models.kubevirt_storage import NativeReportingTarget  # noqa: F401

    with sa.create_engine("sqlite://").begin() as connection:
        connection.exec_driver_sql("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO projects VALUES ('unchanged')")
        module = migration(filename)
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert native | {"projects"} == set(sa.inspect(connection).get_table_names())
        for name in native:
            assert not sa.inspect(connection).get_foreign_keys(name)
            assert {c.name for c in Base.metadata.tables[name].columns} == {
                c["name"] for c in sa.inspect(connection).get_columns(name)
            }
        module.downgrade()
        assert set(sa.inspect(connection).get_table_names()) == {"projects"}
        assert (
            connection.exec_driver_sql("SELECT id FROM projects").scalar()
            == "unchanged"
        )


@pytest.mark.parametrize("filename", MIGRATIONS)
def test_native_reporting_postgresql_ddl(filename):
    output = io.StringIO()
    module = migration(filename)
    module.op = Operations(
        MigrationContext.configure(
            dialect=postgresql.dialect(), opts={"as_sql": True, "output_buffer": output}
        )
    )
    module.upgrade()
    module.downgrade()
    assert "JSONB" in output.getvalue()
    assert "UUID" in output.getvalue()
    assert "CASCADE" not in output.getvalue()
