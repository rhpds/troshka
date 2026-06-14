import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models import *  # noqa: F403 — ensure all models register with Base

test_engine = create_engine(
    "sqlite:///./test.db", connect_args={"check_same_thread": False}
)
Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)
TestSession = sessionmaker(bind=test_engine)


def get_test_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
