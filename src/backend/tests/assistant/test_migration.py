import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_assistant_migration_round_trip():
    path = (
        Path(__file__).parents[2]
        / "alembic/versions/b7a260112601_add_project_assistant.py"
    )
    spec = importlib.util.spec_from_file_location("assistant_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = sa.inspect(connection)
        assert len(inspector.get_table_names()) == 5
        unique = inspector.get_unique_constraints("assistant_messages")
        assert unique[0]["column_names"] == ["conversation_id", "request_id", "role"]
        migration.downgrade()
        assert sa.inspect(connection).get_table_names() == []
