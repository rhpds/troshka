from sqlalchemy import text

from app.core.database import Base, engine, get_db


def test_base_class_exists():
    assert hasattr(Base, "metadata")


def test_engine_connects():
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_get_db_yields_session():
    gen = get_db()
    session = next(gen)
    assert session is not None
    try:
        next(gen)
    except StopIteration:
        pass
