"""Run only the additive migration against disposable metadata."""

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
        / "alembic/versions/d93b8a6c1042_add_managed_groups.py"
    )
    spec = importlib.util.spec_from_file_location("groups_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_downgrade_sqlite():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys=ON"))
        connection.execute(sa.text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"))
        module = migration()
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        inspector = sa.inspect(connection)
        assert "managed_groups" in inspector.get_table_names()
        assert (
            len(
                inspector.get_pk_constraint("managed_group_managers")[
                    "constrained_columns"
                ]
            )
            == 2
        )
        for table in ("managed_group_members", "managed_group_managers"):
            assert {
                fk["options"]["ondelete"] for fk in inspector.get_foreign_keys(table)
            } == {"CASCADE"}
        module.downgrade()
        assert sa.inspect(connection).get_table_names() == ["users"]


def test_postgresql_ddl_compiles():
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
    assert "UUID" in ddl
    assert "ON DELETE CASCADE" in ddl
    assert "DROP TABLE managed_groups" in ddl
