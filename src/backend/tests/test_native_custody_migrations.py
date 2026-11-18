"""Exercise deployment DDL and predecessor uniqueness on a disposable database."""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests import test_quota_concurrency

postgres = test_quota_concurrency.postgres


def origin_migrations():
    """The accumulated candidate chain, loaded without an application engine."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "alembic")
    )
    scripts = ScriptDirectory.from_config(config)
    revisions = list(
        reversed(list(scripts.iterate_revisions("f17ae65bd394", "ab841d2e6109")))
    )
    assert revisions[0].down_revision == "ab841d2e6109"
    assert revisions[-1].revision == "f17ae65bd394"
    return [revision.module for revision in revisions]


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
def test_accumulated_origin_migrations_and_immutable_cleanup_evidence(postgres):
    """Execute real Alembic DDL twice and retain evidence after Project removal."""
    _, (project_ids, _, _, _), engine = postgres
    migrations = origin_migrations()
    with engine.begin() as connection, Operations.context(
        MigrationContext.configure(connection)
    ):
        before = set(inspect(connection).get_table_names())
        # The isolated fixture creates model tables; remove only this migration slice.
        # Model creation does not install this function, which the first downgrade drops.
        connection.execute(
            text(
                "CREATE FUNCTION reject_native_origin_mutation() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'bootstrap only'; END $$"
            )
        )
        for migration in reversed(migrations):
            migration.downgrade()
        assert (
            connection.execute(
                text("SELECT to_regprocedure('reject_native_origin_mutation()')")
            ).scalar()
            is None
        )
        predecessor = set(inspect(connection).get_table_names())
        removed = before - predecessor
        assert "native_project_namespace_evidence" in removed
        for migration in migrations:
            migration.upgrade()
        assert set(inspect(connection).get_table_names()) == before

        metadata = MetaData()
        cleanup = Table(
            "native_project_namespace_cleanups", metadata, autoload_with=connection
        )
        evidence = Table(
            "native_project_namespace_evidence", metadata, autoload_with=connection
        )
        assert not inspect(connection).get_foreign_keys(cleanup.name)
        assert not inspect(connection).get_foreign_keys(evidence.name)
        cleanup_id, evidence_id = str(uuid4()), str(uuid4())
        connection.execute(
            cleanup.insert().values(
                id=cleanup_id,
                project_id=project_ids[0],
                digest="a" * 64,
                payload={"phase": "retained"},
            )
        )
        connection.execute(
            evidence.insert().values(
                id=evidence_id,
                cleanup_id=cleanup_id,
                phase="namespace_absent",
                digest="b" * 64,
                payload={"namespace_uid": "original"},
            )
        )
        for mutation in (evidence.update().values(digest="c" * 64), evidence.delete()):
            with pytest.raises(
                DBAPIError, match="immutable"
            ), connection.begin_nested():
                connection.execute(mutation.where(evidence.c.id == evidence_id))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                evidence.insert().values(
                    id=str(uuid4()),
                    cleanup_id=cleanup_id,
                    phase="namespace_absent",
                    digest="c" * 64,
                    payload={},
                )
            )
        # State is deliberately mutable while phase evidence remains append-only.
        connection.execute(
            cleanup.update()
            .where(cleanup.c.id == cleanup_id)
            .values(payload={"phase": "settled"})
        )
        connection.execute(
            text("DELETE FROM projects WHERE id=:id"), {"id": project_ids[0]}
        )
        assert connection.execute(evidence.select()).one().payload == {
            "namespace_uid": "original"
        }
        assert connection.execute(cleanup.select()).one().payload == {
            "phase": "settled"
        }

        for migration in reversed(migrations):
            migration.downgrade()
        assert set(inspect(connection).get_table_names()) == predecessor
        assert (
            connection.execute(
                text("SELECT to_regprocedure('reject_native_origin_mutation()')")
            ).scalar()
            is None
        )
        for migration in migrations:
            migration.upgrade()
        assert set(inspect(connection).get_table_names()) == before
        # A second upgrade really restored the trigger rather than only the table.
        connection.execute(
            evidence.insert().values(
                id=evidence_id,
                cleanup_id=cleanup_id,
                phase="namespace_absent",
                digest="b" * 64,
                payload={},
            )
        )
        with pytest.raises(DBAPIError, match="immutable"), connection.begin_nested():
            connection.execute(evidence.delete().where(evidence.c.id == evidence_id))


@pytest.mark.skipif(
    not os.getenv("QUOTA_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL URL not configured",
)
@pytest.mark.parametrize("retirement", [False, True])
def test_custody_successor_migration_roundtrip(postgres, retirement):
    _, _, engine = postgres
    filename = (
        "d90bc31e7426_native_binding_retirement.py"
        if retirement
        else "c81fa62d9730_native_custody_successors.py"
    )
    request_table = (
        "native_binding_retirement_requests"
        if retirement
        else "native_custody_reconciliation_requests"
    )
    receipt_table = (
        "native_binding_retirement_receipts"
        if retirement
        else "native_custody_successors"
    )
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location("custody_successor_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection, Operations.context(
        MigrationContext.configure(connection)
    ):
        migration.downgrade()
        migration.upgrade()
        names = inspect(connection).get_table_names()
        assert request_table in names
        assert receipt_table in names
        table = Table(receipt_table, MetaData(), autoload_with=connection)
        values = dict(
            project_id=str(uuid4()),
            predecessor_id=str(uuid4()),
            predecessor_digest="a" * 64,
            digest="b" * 64,
            payload={},
        )
        if retirement:
            values["request_id"] = values.pop("predecessor_id")
            values.pop("predecessor_digest")
        connection.execute(table.insert().values(id=str(uuid4()), **values))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(table.insert().values(id=str(uuid4()), **values))
        migration.downgrade()
        names = inspect(connection).get_table_names()
        assert receipt_table not in names
        assert request_table not in names
