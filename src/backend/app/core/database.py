import logging
import os
from collections.abc import Generator
from typing import cast

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from app.core.config import config

_log = logging.getLogger(__name__)


def _db_pool_int(name: str, default: int) -> int:
    """Read a per-process DB pool setting from config (env-overridable via
    ``TROSHKA_DATABASE__<NAME>``). Kept small by default: the total connection
    budget is (workers x worker pool) + (backend pool) + overhead, and Postgres
    ``max_connections`` is sized to match in the Helm chart. An oversized pool
    (the old 50/100) let a few processes exhaust the DB; workers override this to
    a tiny pool since each RQ worker runs one job at a time."""
    db_cfg = getattr(config, "database", None)
    try:
        return int(getattr(db_cfg, name, default))
    except (TypeError, ValueError):
        return default


def _sqlite_create_engine(url: str):
    """SQLite engine for tests.

    QueuePool on a file DB lets several connections sit on one sqlite file and
    deadlock (``database is locked`` until pytest-timeout). In-memory needs
    StaticPool (shared connection). File tests use NullPool so a closed session
    actually drops the sqlite lock. ``timeout`` is sqlite busy-wait (seconds).
    """
    connect_args = {"check_same_thread": False, "timeout": 5}
    memory = url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url
    if memory:
        return create_engine(url, connect_args=connect_args, poolclass=StaticPool)
    return create_engine(url, connect_args=connect_args, poolclass=NullPool)


def _create_engine():
    """Build the process engine. Postgres uses QueuePool; SQLite is test-only."""
    url = str(config.database.url)
    if url.startswith("sqlite"):
        return _sqlite_create_engine(url)
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=_db_pool_int("pool_size", 5),
        max_overflow=_db_pool_int("max_overflow", 10),
        pool_timeout=_db_pool_int("pool_timeout", 30),
        # Recycle idle connections so a burst of activity doesn't leave connections
        # parked idle for hours (which, with a large fleet, exhausts max_connections).
        pool_recycle=_db_pool_int("pool_recycle", 1800),
    )


engine = _create_engine()


@event.listens_for(engine, "connect")
def _sqlite_connect(dbapi_conn, _connection_record):
    """WAL + busy timeout so TestClient / deploy threads don't exclusive-lock forever."""
    if not str(config.database.url).startswith("sqlite"):
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
    finally:
        cur.close()


@event.listens_for(engine, "checkout")
def _on_checkout(_dbapi_conn, _connection_record, _connection_proxy):
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        return
    pool = cast(QueuePool, pool)
    _log.debug(
        "DB pool: %d/%d checked out, %d overflow",
        pool.checkedout(),
        pool.size(),
        pool.overflow(),
    )


@event.listens_for(engine, "checkin")
def _on_checkin(_dbapi_conn, _connection_record):
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        return
    pool = cast(QueuePool, pool)
    if pool.checkedout() > pool.size():
        _log.warning(
            "DB pool pressure: %d/%d checked out, %d overflow",
            pool.checkedout(),
            pool.size(),
            pool.overflow(),
        )


SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    import app.models  # noqa: F401

    _run_migrations()
    Base.metadata.create_all(bind=engine)


def _run_migrations():
    try:
        from alembic.config import Config

        from alembic import command

        alembic_dir = os.path.join(os.path.dirname(__file__), "..", "..")
        alembic_cfg = Config(os.path.join(alembic_dir, "alembic.ini"))
        alembic_cfg.set_main_option(
            "script_location", os.path.join(alembic_dir, "alembic")
        )
        alembic_cfg.set_main_option("sqlalchemy.url", config.database.url)
        command.upgrade(alembic_cfg, "head")
        _log.info("Database migrations applied successfully")
    except Exception as exc:
        _log.warning("Alembic migration skipped: %s", exc)
