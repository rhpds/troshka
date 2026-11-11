"""One-time legacy access import and explicit initial-admin provisioning.

Login authorization never calls this module. After the import marker is saved,
configuration and OpenShift groups no longer influence admission or roles.
"""

import argparse
import logging
import threading
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import func, inspect, text

from app.core.config import config
from app.core.database import SessionLocal
from app.models.system_config import SystemConfig
from app.models.user import User

logger = logging.getLogger(__name__)
MARKER = "database_access_initialized_v1"


def guard_account_change(db, actor, user, *, role=None, access_enabled=None):
    _lock(db)
    actor = db.get(User, actor.id, populate_existing=True)
    db.refresh(user)
    if actor is None or not actor.access_enabled or actor.role != "admin":
        raise HTTPException(403, "Administrator access is no longer available")
    removes_admin = role not in (None, "admin") or access_enabled is False
    if user.role == "admin" and user.access_enabled and removes_admin:
        count = db.query(User).filter_by(role="admin", access_enabled=True).count()
        if count <= 1:
            raise HTTPException(409, "Cannot remove the last enabled administrator")


def _lock(db):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(724826)"))


def _csv(name: str) -> set[str]:
    return {
        value.strip().lower()
        for value in str(getattr(config.auth, name, "")).split(",")
        if value.strip()
    }


def _legacy_policy():
    policy = {
        key: _csv(key)
        for key in (
            "allowed_users",
            "admin_users",
            "operator_users",
            "allowed_groups",
            "admin_groups",
            "operator_groups",
        )
    }
    groups = {}
    if any(
        policy[key] for key in ("allowed_groups", "admin_groups", "operator_groups")
    ):
        from app.core.auth import _get_k8s_client

        api = _get_k8s_client()
        if api is None:
            raise RuntimeError(
                "OpenShift group lookup unavailable; legacy access import deferred"
            )
        response = api.list_cluster_custom_object(
            "user.openshift.io", "v1", "groups", _request_timeout=(3, 10)
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("items"), list
        ):
            raise RuntimeError("Invalid OpenShift group response")
        items = cast(list[dict[str, Any]], response["items"])
        assert isinstance(items, list)
        groups = {
            group["metadata"]["name"].lower(): set(group.get("users") or [])
            for group in items
        }
    return policy, groups


def _members(groups, names):
    return set().union(*(groups.get(name, set()) for name in names))


def _legacy_admission(email, policy, groups):
    if policy["allowed_groups"]:
        names = (
            policy["allowed_groups"]
            | policy["admin_groups"]
            | policy["operator_groups"]
        )
        return (
            email in _members(groups, names) or email.lower() in policy["allowed_users"]
        )
    return not policy["allowed_users"] or email.lower() in policy["allowed_users"]


def _legacy_role(email, policy, groups):
    if email.lower() in policy["admin_users"]:
        return "admin"
    if email.lower() in policy["operator_users"]:
        return "operator"
    if email in _members(groups, policy["admin_groups"]):
        return "admin"
    if email in _members(groups, policy["operator_groups"]):
        return "operator"
    return "user"


def import_legacy_access() -> bool:
    with SessionLocal() as db:
        _lock(db)
        if db.get(SystemConfig, MARKER):
            return True
        policy, groups = _legacy_policy()
        users = {user.email: user for user in db.query(User).all()}
        names = (
            policy["allowed_groups"]
            | policy["admin_groups"]
            | policy["operator_groups"]
        )
        if any("@" not in identity for identity in _members(groups, names)):
            raise RuntimeError(
                "Legacy groups contain non-email identities. Map access and roles "
                "to explicit email allowlists before upgrading; import not committed."
            )
        identities = set(users) | _members(groups, names)
        identities |= (
            policy["allowed_users"] | policy["admin_users"] | policy["operator_users"]
        )
        enabled_admins = 0
        for identity in sorted(identities):
            allowed = _legacy_admission(identity, policy, groups)
            if not allowed and identity not in users:
                continue
            user = users.get(identity)
            if user is None:
                user = User(email=identity, auth_source="invited")
                db.add(user)
            user.access_enabled = allowed
            user.role = _legacy_role(identity, policy, groups)
            enabled_admins += int(allowed and user.role == "admin")
        if not enabled_admins:
            # A fresh dedicated instance is seeded by the provisioning command.
            db.rollback()
            return False
        db.add(SystemConfig(key=MARKER, value="legacy-import"))
        db.commit()
        logger.info("Database access initialized from legacy policy")
        return True


def bootstrap_admin(identity: str) -> bool:
    identity = identity.strip().lower()
    if not identity or any(char.isspace() for char in identity):
        raise ValueError("A nonempty initial-admin identity is required")
    with SessionLocal() as db:
        _lock(db)
        if db.get(SystemConfig, MARKER):
            return False
        matches = (
            db.query(User).filter(func.lower(User.email) == identity).limit(2).all()
        )
        if len(matches) > 1:
            raise ValueError(
                "Initial-admin identity matches multiple existing accounts"
            )
        user = matches[0] if matches else None
        created = user is None
        if user is not None and (not user.access_enabled or user.role != "admin"):
            raise ValueError(
                "Initial-admin identity exists without enabled admin access"
            )
        if created:
            db.add(
                User(
                    email=identity,
                    role="admin",
                    auth_source="invited",
                    access_enabled=True,
                )
            )
        if not db.get(SystemConfig, MARKER):
            db.add(SystemConfig(key=MARKER, value="provisioned-admin"))
        db.commit()
        return created


def initialize_local_access():
    """Preserve legacy dev accounts once, without changing SSO accounts or roles."""
    if config.auth.oauth_enabled:
        raise RuntimeError("Local access initialization requires SSO to be disabled")
    with SessionLocal() as db:
        _lock(db)
        if db.get(SystemConfig, MARKER):
            return
        for user in db.query(User).filter_by(auth_source="dev").all():
            user.access_enabled = True
        db.add(SystemConfig(key=MARKER, value="local-dev-import"))
        db.commit()


def start_access_bootstrap() -> threading.Event:
    stop = threading.Event()
    if not config.auth.oauth_enabled:
        initialize_local_access()
        return stop

    def run():
        while not stop.is_set():
            try:
                if import_legacy_access():
                    return
            except Exception:
                logger.warning(
                    "Database access initialization pending; retrying", exc_info=True
                )
            stop.wait(30)

    threading.Thread(target=run, name="database-access-bootstrap", daemon=True).start()
    return stop


def prepare_database():
    from pathlib import Path

    from alembic.config import Config

    import app.models  # noqa: F401
    from alembic import command
    from app.core.database import Base, engine

    root = Path(__file__).resolve().parents[2]
    migration_config = Config(str(root / "alembic.ini"))
    migration_config.set_main_option("script_location", str(root / "alembic"))
    if not inspect(engine).has_table("users"):
        Base.metadata.create_all(bind=engine)
        command.stamp(migration_config, "head")
    else:
        command.upgrade(migration_config, "head")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed the initial Troshka admin without resetting existing accounts"
    )
    parser.add_argument("--admin")
    parser.add_argument("--prepare-db", action="store_true")
    args = parser.parse_args()
    if args.prepare_db:
        prepare_database()
    if args.admin:
        created = bootstrap_admin(args.admin)
        print(
            "Initial admin created" if created else "Existing access policy preserved"
        )
    elif not args.prepare_db:
        parser.error("Specify --admin or --prepare-db")
