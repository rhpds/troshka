from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import ws
from app.api.ws import _authenticate_ws
from app.core import auth
from app.core.database import Base, get_db
from app.main import app
from app.models.api_key import ApiKey, hash_key
from app.models.user import User
from app.services import database_access as access
from app.tunnel.auth import _resolve_user

ADMIN_ID = "00000000-0000-0000-0000-000000000001"
DISABLED_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def state(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    settings = SimpleNamespace(
        auth=SimpleNamespace(
            oauth_enabled=True,
            allowed_users="",
            admin_users="",
            operator_users="",
            allowed_groups="",
            admin_groups="",
            operator_groups="",
            jwt_secret="database-access-test-secret-fixture-only",
            jwt_algorithm="HS256",
            jwt_expiry_hours=24,
        )
    )
    monkeypatch.setattr(access, "config", settings)
    monkeypatch.setattr(auth, "config", settings)
    monkeypatch.setattr(ws, "config", settings)
    monkeypatch.setattr(access, "SessionLocal", sessions)
    old = dict(app.dependency_overrides)

    def get_session():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = get_session
    with sessions() as db:
        db.add(
            User(
                id=ADMIN_ID,
                email="admin@example.com",
                role="admin",
                auth_source="sso",
                access_enabled=True,
            )
        )
        db.commit()
    yield sessions, settings, TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(old)
    engine.dispose()


def headers(email="admin@example.com"):
    return {"X-Forwarded-Email": email, "X-Forwarded-User": email}


def test_unknown_sso_identity_is_denied_without_registration(state):
    sessions, _, client = state
    assert (
        client.get(
            "/api/v1/auth/me", headers=headers("unknown@example.com")
        ).status_code
        == 403
    )
    with sessions() as db:
        assert db.query(User).count() == 1


def test_invitation_links_and_role_remains_database_owned(state, monkeypatch):
    sessions, settings, client = state
    settings.auth.allowed_users = "nobody@example.com"
    monkeypatch.setattr(
        auth, "_resolve_role", lambda *args: pytest.fail("External role lookup")
    )
    response = client.post(
        "/api/v1/users/",
        headers=headers(),
        json={"email": "Invite@example.com", "role": "operator"},
    )
    assert response.status_code == 201
    identity = client.get("/api/v1/auth/me", headers=headers("INVITE@example.com"))
    assert identity.status_code == 200
    assert identity.json()["id"] == response.json()["id"]
    assert identity.json()["role"] == "operator"
    with sessions() as db:
        assert (
            db.query(User).filter_by(email="invite@example.com").one().auth_source
            == "sso"
        )


def test_disabled_user_is_denied_for_sso_jwt_and_websockets(state):
    sessions, _, client = state
    with sessions() as db:
        user = User(
            id=DISABLED_ID,
            email="disabled@example.com",
            role="admin",
            access_enabled=False,
        )
        db.add(user)
        db.commit()
    token = auth.create_jwt(DISABLED_ID, "disabled@example.com", "admin")
    assert (
        client.get(
            "/api/v1/auth/me", headers=headers("disabled@example.com")
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 403
    )
    with sessions() as db:
        assert _authenticate_ws(token, db) is None
        assert (
            _authenticate_ws(
                None, db, headers={"x-forwarded-email": "disabled@example.com"}
            )
            is None
        )


def test_role_edits_and_disabling_apply_on_next_request(state):
    _, _, client = state
    response = client.post(
        "/api/v1/users/", headers=headers(), json={"email": "person@example.com"}
    )
    path = f"/api/v1/users/{response.json()['id']}"
    assert (
        client.patch(path, headers=headers(), json={"role": "operator"}).status_code
        == 200
    )
    assert (
        client.get("/api/v1/auth/me", headers=headers("person@example.com")).json()[
            "role"
        ]
        == "operator"
    )
    assert (
        client.patch(
            path, headers=headers(), json={"access_enabled": False}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/auth/me", headers=headers("person@example.com")).status_code
        == 403
    )
    assert (
        client.patch(path, headers=headers(), json={"access_enabled": True}).status_code
        == 200
    )
    assert (
        client.get("/api/v1/auth/me", headers=headers("person@example.com")).status_code
        == 200
    )


def test_admin_cannot_disable_self_and_users_cannot_grant_access(state):
    _, _, client = state
    assert (
        client.patch(
            f"/api/v1/users/{ADMIN_ID}",
            headers=headers(),
            json={"access_enabled": False},
        ).status_code
        == 400
    )
    client.post(
        "/api/v1/users/", headers=headers(), json={"email": "person@example.com"}
    )
    assert (
        client.post(
            "/api/v1/users/",
            headers=headers("person@example.com"),
            json={"email": "attacker@example.com", "role": "admin"},
        ).status_code
        == 403
    )


def test_disabled_user_api_key_cannot_authenticate_http_ws_or_tunnel(state):
    sessions, _, client = state
    token = "trk_database_access_test"
    with sessions() as db:
        db.add(
            User(
                id=DISABLED_ID,
                email="disabled@example.com",
                role="user",
                access_enabled=False,
            )
        )
        db.flush()
        db.add(
            ApiKey(
                user_id=DISABLED_ID,
                name="test",
                key_hash=hash_key(token),
                key_prefix="trk_test",
                is_active=True,
            )
        )
        db.commit()
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 403
    )
    with sessions() as db:
        assert _authenticate_ws(token, db) is None
        assert _resolve_user(token, db) is None


def test_bootstrap_admin_creates_once_and_preserves_later_changes(state):
    sessions, _, _ = state
    assert access.bootstrap_admin("requester@example.com")
    with sessions() as db:
        user = db.query(User).filter_by(email="requester@example.com").one()
        assert user.role == "admin" and user.access_enabled
        user.role = "user"
        user.access_enabled = False
        db.commit()
    assert not access.bootstrap_admin("requester@example.com")
    with sessions() as db:
        user = db.query(User).filter_by(email="requester@example.com").one()
        assert user.role == "user" and not user.access_enabled


def test_bootstrap_does_not_restore_deleted_admin(state):
    sessions, _, _ = state
    assert access.bootstrap_admin("requester@example.com")
    with sessions() as db:
        db.query(User).filter_by(email="requester@example.com").delete()
        db.commit()
    assert not access.bootstrap_admin("requester@example.com")
    with sessions() as db:
        assert db.query(User).filter_by(email="requester@example.com").first() is None


def test_bootstrap_rejects_existing_disabled_account_without_marking_complete(state):
    sessions, _, _ = state
    with sessions() as db:
        db.get(User, ADMIN_ID).access_enabled = False
        db.commit()
    with pytest.raises(ValueError, match="without enabled admin"):
        access.bootstrap_admin("admin@example.com")
    with sessions() as db:
        assert db.get(access.SystemConfig, access.MARKER) is None


def test_access_migration_preserves_existing_rows_and_enables_new_rows():
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    path = (
        Path(__file__).parents[1]
        / "alembic/versions/e47c2a9b6103_add_database_access.py"
    )
    spec = importlib.util.spec_from_file_location("access_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE users (id INTEGER PRIMARY KEY, role TEXT)")
        )
        connection.execute(text("INSERT INTO users VALUES (1, 'admin')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.execute(text("INSERT INTO users (id, role) VALUES (2, 'user')"))
        assert connection.execute(
            text("SELECT id, role, access_enabled FROM users ORDER BY id")
        ).all() == [(1, "admin", 0), (2, "user", 1)]
    engine.dispose()


def test_legacy_import_filters_revoked_accounts_and_runs_once(state, monkeypatch):
    sessions, settings, _ = state
    settings.auth.allowed_groups = "troshka-users"
    settings.auth.admin_users = "admin@example.com"
    api = MagicMock()
    api.list_cluster_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "troshka-users"},
                "users": ["admin@example.com", "new@example.com"],
            }
        ]
    }
    monkeypatch.setattr(auth, "_get_k8s_client", lambda: api)
    with sessions() as db:
        db.add(User(email="revoked@example.com", role="admin", access_enabled=False))
        db.commit()
    assert access.import_legacy_access()
    with sessions() as db:
        assert (
            not db.query(User)
            .filter_by(email="revoked@example.com")
            .one()
            .access_enabled
        )
        assert db.query(User).filter_by(email="new@example.com").one().access_enabled
        admin = db.get(User, ADMIN_ID)
        admin.role = "user"
        db.commit()
    assert access.import_legacy_access()
    with sessions() as db:
        assert db.get(User, ADMIN_ID).role == "user"
    assert api.list_cluster_custom_object.call_count == 1


def test_legacy_lookup_failure_does_not_complete_import(state, monkeypatch):
    _, settings, _ = state
    settings.auth.allowed_groups = "troshka-users"
    monkeypatch.setattr(auth, "_get_k8s_client", lambda: None)
    with pytest.raises(RuntimeError):
        access.import_legacy_access()


def test_ambiguous_case_insensitive_identity_is_denied(state):
    sessions, _, _ = state
    with sessions() as db:
        db.add_all([User(email="Case@example.com"), User(email="case@example.com")])
        db.commit()
        assert auth.find_enabled_user(db, "CASE@example.com") is None
        assert (
            auth.find_enabled_user(db, "Case@example.com").email == "Case@example.com"
        )


def test_local_startup_migrates_dev_accounts_once(state):
    sessions, settings, _ = state
    settings.auth.oauth_enabled = False
    with sessions() as db:
        db.add(
            User(
                email="local-dev@troshka",
                role="admin",
                auth_source="dev",
                access_enabled=False,
            )
        )
        db.add(User(email="other@example.com", auth_source="sso", access_enabled=False))
        db.commit()
    access.start_access_bootstrap().set()
    with sessions() as db:
        local = db.query(User).filter_by(email="local-dev@troshka").one()
        assert local.access_enabled
        assert local.role == "admin"
        assert (
            not db.query(User).filter_by(email="other@example.com").one().access_enabled
        )
        local.access_enabled = False
        db.commit()
    access.start_access_bootstrap().set()
    with sessions() as db:
        assert (
            not db.query(User).filter_by(email="local-dev@troshka").one().access_enabled
        )


def test_fresh_local_startup_creates_enabled_admin(state):
    sessions, settings, _ = state
    settings.auth.oauth_enabled = False
    with sessions() as db:
        db.query(User).delete()
        db.commit()
    access.start_access_bootstrap().set()
    with sessions() as db:
        user = auth._get_or_create_dev_user(db)
        assert user.access_enabled
        assert user.role == "admin"
        assert db.get(access.SystemConfig, access.MARKER) is not None


def test_disabled_dev_account_cannot_bypass_access(state, monkeypatch):
    from app.api import auth as auth_api

    sessions, settings, client = state
    settings.auth.oauth_enabled = False
    monkeypatch.setattr(auth_api, "config", settings)
    with sessions() as db:
        db.add(
            User(
                email="local-dev@troshka",
                role="admin",
                auth_source="dev",
                access_enabled=False,
            )
        )
        db.commit()
        assert _authenticate_ws(None, db) is None
    assert client.get("/api/v1/auth/me").status_code == 403
    assert client.get("/api/v1/auth/dev-token").status_code == 403
    assert client.get("/api/v1/auth/dev-token/admin").status_code == 403


def test_dev_login_preserves_database_role(state, monkeypatch):
    from app.api import auth as auth_api

    sessions, settings, client = state
    settings.auth.oauth_enabled = False
    monkeypatch.setattr(auth_api, "config", settings)
    with sessions() as db:
        db.add(User(email="local-dev@troshka", role="user", auth_source="dev"))
        db.commit()
    assert client.get("/api/v1/auth/dev-token").json()["role"] == "user"
    assert client.get("/api/v1/auth/dev-token/admin").status_code == 403


def test_legacy_username_group_requires_explicit_mapping(state, monkeypatch):
    sessions, settings, _ = state
    settings.auth.allowed_groups = "troshka-users"
    settings.auth.admin_users = "admin@example.com"
    api = MagicMock()
    api.list_cluster_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "troshka-users"},
                "users": ["admin@example.com", "alice"],
            }
        ]
    }
    monkeypatch.setattr(auth, "_get_k8s_client", lambda: api)
    with pytest.raises(RuntimeError, match="non-email"):
        access.import_legacy_access()
    with sessions() as db:
        assert db.get(access.SystemConfig, access.MARKER) is None
        assert db.query(User).filter_by(email="alice").first() is None
