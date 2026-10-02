import atexit
import os
import sys

_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

# Per-process file DB + a SINGLE SQLAlchemy engine (SessionLocal). Previously
# conftest built a second engine on the same sqlite:///./test.db file while the
# app used QueuePool — two engines → "database is locked" deadlocks that hung
# until pytest-timeout (flaky on slow GitLab ALM shards).
_TEST_DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), f"test-{os.getpid()}.db")
)
os.environ["TROSHKA_DATABASE__URL"] = f"sqlite:///{_TEST_DB_PATH}"


def _cleanup_test_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = _TEST_DB_PATH + suffix
        try:
            os.remove(path)
        except OSError:
            pass


atexit.register(_cleanup_test_db)

from sqlalchemy.dialects import sqlite

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base, SessionLocal, engine
from app.models import *  # noqa: F403 — ensure all models register with Base

# Same engine the app uses — do not create a second one on this file.
test_engine = engine
TestSession = SessionLocal
Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)


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


# Valid OCP harvest creds for tests (matches kubeconfig_merge validators).


def sample_kubeadmin_password() -> str:
    """Openshift-install-shaped password that passes is_valid_kubeadmin_password."""
    return "-".join(["testpw", "abcd", "efgh", "ijkl"])


def sample_kubeconfig_yaml(server: str = "https://api.test:6443") -> str:
    import yaml

    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "test",
                    "cluster": {
                        "server": server,
                        "certificate-authority-data": "Q0E=",
                    },
                }
            ],
            "users": [{"name": "admin", "user": {"token": "sha256~abc"}}],
            "contexts": [
                {
                    "name": "test",
                    "context": {
                        "cluster": "test",
                        "user": "admin",
                        "namespace": "default",
                    },
                }
            ],
            "current-context": "test",
        }
    )
