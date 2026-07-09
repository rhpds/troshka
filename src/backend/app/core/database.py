import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import config

_log = logging.getLogger(__name__)

engine = create_engine(
    config.database.url, pool_pre_ping=True, pool_size=20, max_overflow=30
)


@event.listens_for(engine, "checkout")
def _on_checkout(dbapi_conn, connection_record, connection_proxy):
    pool = engine.pool
    _log.debug(
        "DB pool: %d/%d checked out, %d overflow",
        pool.checkedout(),
        pool.size(),
        pool.overflow(),
    )


@event.listens_for(engine, "checkin")
def _on_checkin(dbapi_conn, connection_record):
    pool = engine.pool
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

    Base.metadata.create_all(bind=engine)
