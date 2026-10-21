"""The disposable test database must retain real connection isolation."""

import os
from pathlib import Path

from tests.conftest import _TEST_DB_PATH, test_engine


def test_database_location_and_cross_connection_transactions():
    configured_dir = os.environ.get("TROSHKA_TEST_DB_DIR")
    if configured_dir:
        assert Path(_TEST_DB_PATH).parent == Path(configured_dir).resolve()
    assert Path(test_engine.url.database).resolve() == Path(_TEST_DB_PATH)

    with test_engine.connect() as writer, test_engine.connect() as reader:
        assert writer.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        writer.exec_driver_sql("CREATE TABLE ci_storage_probe (value INTEGER)")
        writer.commit()
        try:
            writer.exec_driver_sql("INSERT INTO ci_storage_probe VALUES (1)")
            assert (
                reader.exec_driver_sql("SELECT count(*) FROM ci_storage_probe").scalar()
                == 0
            )
            reader.rollback()
            writer.commit()
            assert (
                reader.exec_driver_sql("SELECT count(*) FROM ci_storage_probe").scalar()
                == 1
            )
            reader.rollback()
            writer.exec_driver_sql("INSERT INTO ci_storage_probe VALUES (2)")
            writer.rollback()
            assert (
                reader.exec_driver_sql("SELECT count(*) FROM ci_storage_probe").scalar()
                == 1
            )
            reader.rollback()
        finally:
            writer.exec_driver_sql("DROP TABLE ci_storage_probe")
            writer.commit()
