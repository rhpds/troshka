import os
import sys

_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

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


import pytest  # noqa: E402

# Live/real-install tests legitimately run long (a tier2 OCP install is ~30-60
# min), so exempt them from the default per-test timeout. Everything else keeps
# the timeout as a guardrail: an accidental real network call (e.g. a troshkad
# HTTP request against a fake test host) then fails loudly at the timeout with a
# stack trace instead of silently hanging the whole suite for minutes.
_TIMEOUT_EXEMPT_MARKERS = {"tier2", "live_env", "live_troshkad", "live_kubevirt"}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if _TIMEOUT_EXEMPT_MARKERS & {m.name for m in item.iter_markers()}:
            item.add_marker(pytest.mark.timeout(0))
