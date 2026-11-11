"""Additive reporting migration, SQLite execution and PostgreSQL DDL."""

import importlib.util
import io
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql


def migration():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/7eacbdd6fe04_native_storage_reporting_enrollment_and_.py"
    )
    spec = importlib.util.spec_from_file_location("native_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_reporting_additive_migration_and_downgrade():
    from app.core.database import Base
    from app.models.kubevirt_storage import NativeReportingTarget  # noqa: F401

    native = {name for name in Base.metadata.tables if name.startswith("native_")}
    with sa.create_engine("sqlite://").begin() as connection:
        connection.exec_driver_sql("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO projects VALUES ('unchanged')")
        module = migration()
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert native <= set(sa.inspect(connection).get_table_names())
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


def test_native_reporting_postgresql_ddl():
    output = io.StringIO()
    module = migration()
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
