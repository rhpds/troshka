import logging
import os
from collections.abc import Generator
from typing import cast

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool

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


engine = create_engine(
    config.database.url,
    pool_pre_ping=True,
    pool_size=_db_pool_int("pool_size", 5),
    max_overflow=_db_pool_int("max_overflow", 10),
    pool_timeout=_db_pool_int("pool_timeout", 30),
    # Recycle idle connections so a burst of activity doesn't leave connections
    # parked idle for hours (which, with a large fleet, exhausts max_connections).
    pool_recycle=_db_pool_int("pool_recycle", 1800),
)


@event.listens_for(engine, "checkout")
def _on_checkout(dbapi_conn, connection_record, connection_proxy):
    pool = cast(QueuePool, engine.pool)
    _log.debug(
        "DB pool: %d/%d checked out, %d overflow",
        pool.checkedout(),
        pool.size(),
        pool.overflow(),
    )


@event.listens_for(engine, "checkin")
def _on_checkin(dbapi_conn, connection_record):
    pool = cast(QueuePool, engine.pool)
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
