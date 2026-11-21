import datetime
import logging

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import config
from app.core.database import get_db
from app.services.access_window import user_within_access_window

logger = logging.getLogger(__name__)


def _parse_csv(value: str) -> set[str]:
    if not value:
        return set()
    return {v.strip().lower() for v in str(value).split(",") if v.strip()}


_admin_users = _parse_csv(getattr(config.auth, "admin_users", ""))
_allowed_users = _parse_csv(getattr(config.auth, "allowed_users", ""))
_operator_users = _parse_csv(getattr(config.auth, "operator_users", ""))


def role_for_email(email: str) -> str:
    email_lower = email.lower()
    if email_lower in _admin_users:
        return "admin"
    if email_lower in _operator_users:
        return "operator"
    return "user"


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    password_bytes = plain.encode("utf-8")
    hashed_bytes = hashed.encode("utf-8")
    return bcrypt.checkpw(password_bytes, hashed_bytes)


def create_jwt(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": datetime.datetime.now(datetime.UTC)
        + datetime.timedelta(hours=getattr(config.auth, "jwt_expiry_hours", 24)),
    }
    return jwt.encode(
        payload, config.auth.jwt_secret, algorithm=config.auth.jwt_algorithm
    )


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(
            token, config.auth.jwt_secret, algorithms=[config.auth.jwt_algorithm]
        )
    except jwt.PyJWTError:
        return None


def _get_user_from_oauth_headers(request: Request) -> dict | None:
    email = request.headers.get("X-Forwarded-Email")
    if not email:
        return None
    return {"email": email, "user": request.headers.get("X-Forwarded-User", email)}


def _get_user_from_jwt(request: Request) -> dict | None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    return decode_jwt(token)


def _upsert_sso_user(email: str, db: Session):
    user = find_enabled_user(db, email)
    if user is None:
        raise HTTPException(
            403, "Access denied. Ask a Troshka administrator for access."
        )
    if user.auth_source == "invited":
        user.auth_source = "sso"
        db.commit()
        db.refresh(user)
    return user


def find_enabled_user(db: Session, identity: str):
    """Prefer exact identities; allow a unique case-insensitive invitation match."""
    from app.models.user import User

    identity = identity.strip()
    user = db.query(User).filter_by(email=identity).first()
    if user is None:
        matches = (
            db.query(User)
            .filter(func.lower(User.email) == identity.lower())
            .limit(2)
            .all()
        )
        user = matches[0] if len(matches) == 1 else None
    if user is None or not user.access_enabled:
        return None
    if not user_within_access_window(user):
        return None
    return user


def _get_or_create_dev_user(db: Session):
    from app.models.user import User

    email = "local-dev@troshka"
    user = db.query(User).filter_by(email=email).first()
    if user is None:
        user = User(
            email=email,
            display_name="local-dev",
            role="admin",
            auth_source="dev",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _get_user_from_api_key(request: Request, db: Session):
    from app.models.api_key import ApiKey, hash_key

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer trk_"):
        return None

    key = auth_header[7:]
    key_hashed = hash_key(key)
    api_key = db.query(ApiKey).filter_by(key_hash=key_hashed, is_active=True).first()
    if not api_key:
        return None

    if api_key.expires_at and api_key.expires_at < datetime.datetime.now(datetime.UTC):
        return None

    api_key.last_used_at = datetime.datetime.now(datetime.UTC)
    db.commit()

    # Surface the matched key so downstream dependencies (e.g.
    # enforce_project_scope) can enforce project scope + permissions.
    request.state.api_key = api_key
    user = api_key.user
    if not user or not user.access_enabled:
        raise HTTPException(403, "Account access is disabled")
    if not user_within_access_window(user):
        raise HTTPException(403, "Account access is outside the allowed date range")
    return user


# Route name -> permission a project-scoped API key must hold to reach it.
# This is an explicit ALLOWLIST: EVERY route not listed here is denied for
# project-scoped keys (global default-deny / least-privilege). Enforcement lives
# in get_current_user, so it applies to every route that authenticates a user —
# no per-router opt-in to forget. Extend as the ops/console pod needs more routes
# (e.g. Plan 6 file/exec-adjacent endpoints).
_SCOPED_KEY_ROUTE_ALLOWLIST: dict[str, str] = {
    "get_project": "topology:read",
    "vm_exec": "vm:exec",
}


def _enforce_scoped_key_access(request: Request):
    """Global default-deny gate for project-scoped API keys.

    Called by ``get_current_user`` right after a ``trk_`` key authenticates. For
    unscoped keys / JWT / OAuth / dev auth this is never reached (only scoped
    keys stash ``is_scoped`` state), so their behavior is unchanged.

    A project-scoped key may only reach routes in
    ``_SCOPED_KEY_ROUTE_ALLOWLIST``, only for its own ``project_id``, and only
    with the required permission — everything else is 403. If no route is
    resolved on the request (``route_name`` empty), it is absent from the
    allowlist and therefore denied (fail closed).
    """
    api_key = getattr(request.state, "api_key", None)
    if api_key is None or not api_key.is_scoped:
        return
    route = request.scope.get("route")
    route_name: str = getattr(route, "name", "") or ""
    perm = _SCOPED_KEY_ROUTE_ALLOWLIST.get(route_name)
    if perm is None:
        raise HTTPException(
            status_code=403, detail="API key is not authorized for this action"
        )
    route_project_id = request.path_params.get("project_id")
    if api_key.project_id != route_project_id or not api_key.has_scope(perm):
        raise HTTPException(
            status_code=403,
            detail="API key is not authorized for this project or action",
        )


def get_current_user(request: Request, db: Session = Depends(get_db)):
    # Default: no API key. _get_user_from_api_key sets this when a trk_ key
    # authenticates, so _enforce_scoped_key_access can distinguish auth paths.
    request.state.api_key = None

    # SSO mode: use OAuth proxy headers
    if config.auth.oauth_enabled:
        user_info = _get_user_from_oauth_headers(request)
        if user_info:
            user = _upsert_sso_user(
                user_info["email"],
                db,
            )
            return user

    # Try API key (trk_ prefix)
    api_key_user = _get_user_from_api_key(request, db)
    if api_key_user:
        _enforce_scoped_key_access(request)
        return api_key_user

    # Try JWT token (works in both dev and SSO mode)
    user_info = _get_user_from_jwt(request)
    if user_info:
        email = user_info.get("email") or user_info.get("sub")
        if email:
            user = find_enabled_user(db, email)
            if user:
                return user
            raise HTTPException(403, "Account access is disabled or unavailable")

    # Dev mode: auto-authenticate as the default admin user
    if not config.auth.oauth_enabled:
        user = _get_or_create_dev_user(db)
        if not user.access_enabled:
            raise HTTPException(403, "Account access is disabled")
        if not user_within_access_window(user):
            raise HTTPException(403, "Account access is outside the allowed date range")
        return user

    raise HTTPException(status_code=401, detail="Not authenticated")


def require_role(min_role: str):
    role_levels = {"user": 0, "operator": 1, "admin": 2}
    required_level = role_levels.get(min_role, 0)

    def dependency(user=Depends(get_current_user)):
        user_level = role_levels.get(user.role, 0)
        if user_level < required_level:
            raise HTTPException(status_code=403, detail=f"Requires {min_role} role")
        return user

    return dependency
