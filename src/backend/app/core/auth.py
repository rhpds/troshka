import datetime
import logging
import time

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import config
from app.core.database import get_db

logger = logging.getLogger(__name__)

# --- OpenShift group resolution ---
_groups_cache: list[dict] = []
_groups_cache_time: float = 0
_GROUPS_CACHE_TTL = 60
_k8s_client = None


def _get_k8s_client():
    global _k8s_client
    if _k8s_client is not None:
        return _k8s_client
    try:
        from kubernetes import client, config as k8s_config

        k8s_config.load_incluster_config()
        _k8s_client = client.CustomObjectsApi()
        logger.info("Kubernetes client initialized for group resolution")
        return _k8s_client
    except Exception:
        logger.debug(
            "Kubernetes client not available — group resolution disabled", exc_info=True
        )
        return None


def _fetch_openshift_groups() -> list[dict]:
    global _groups_cache, _groups_cache_time
    if _groups_cache and time.time() - _groups_cache_time < _GROUPS_CACHE_TTL:
        return _groups_cache

    api = _get_k8s_client()
    if api is None:
        return []

    try:
        result = api.list_cluster_custom_object(
            group="user.openshift.io",
            version="v1",
            plural="groups",
        )
        _groups_cache = result.get("items", [])  # type: ignore[union-attr]
        _groups_cache_time = time.time()
        logger.debug("Fetched %d OpenShift groups", len(_groups_cache))
        return _groups_cache
    except Exception:
        logger.warning("Failed to fetch OpenShift groups", exc_info=True)
        return _groups_cache


def _get_user_groups(username: str, email: str | None = None) -> set[str]:
    groups = _fetch_openshift_groups()
    identities = {username}
    if email:
        identities.add(email)
    return {
        g["metadata"]["name"].lower()
        for g in groups
        if identities & set(g.get("users", []))
    }


def _role_for_groups(username: str, email: str | None = None) -> str | None:
    if not username:
        return None
    user_groups = _get_user_groups(username, email)
    if not user_groups:
        return None
    if _admin_groups and user_groups & _admin_groups:
        return "admin"
    if _operator_groups and user_groups & _operator_groups:
        return "operator"
    if _allowed_groups and user_groups & _allowed_groups:
        return "user"
    return None


def _parse_csv(value: str) -> set[str]:
    if not value:
        return set()
    return {v.strip().lower() for v in str(value).split(",") if v.strip()}


_admin_users = _parse_csv(getattr(config.auth, "admin_users", ""))
_allowed_users = _parse_csv(getattr(config.auth, "allowed_users", ""))
_operator_users = _parse_csv(getattr(config.auth, "operator_users", ""))
_allowed_groups = _parse_csv(getattr(config.auth, "allowed_groups", ""))
_admin_groups = _parse_csv(getattr(config.auth, "admin_groups", ""))
_operator_groups = _parse_csv(getattr(config.auth, "operator_groups", ""))


def role_for_email(email: str) -> str:
    email_lower = email.lower()
    if email_lower in _admin_users:
        return "admin"
    if email_lower in _operator_users:
        return "operator"
    return "user"


def _resolve_role(email: str, ocp_username: str | None) -> str:
    """Resolve user role: email config takes precedence, then group membership."""
    email_lower = email.lower()
    if email_lower in _admin_users:
        return "admin"
    if email_lower in _operator_users:
        return "operator"
    if ocp_username:
        group_role = _role_for_groups(ocp_username, email)
        if group_role:
            return group_role
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
        + datetime.timedelta(hours=config.auth.jwt_expiry_hours),
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


def _upsert_sso_user(
    email: str, display_name: str | None, db: Session, resolved_role: str = "user"
):
    from app.models.user import User

    user = db.query(User).filter_by(email=email).first()
    if user is None:
        user = User(
            email=email,
            display_name=display_name or email.split("@")[0],
            role=resolved_role,
            auth_source="sso",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("Created SSO user %s with role %s", email, user.role)
    else:
        changed = False
        if user.auth_source == "invited":
            user.auth_source = "sso"
            changed = True
        if display_name and not user.display_name:
            user.display_name = display_name
            changed = True
        if user.role != resolved_role:
            logger.info(
                "Updating role for %s: %s -> %s", email, user.role, resolved_role
            )
            user.role = resolved_role
            changed = True
        if changed:
            db.commit()
            db.refresh(user)
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

    return api_key.user


def _enforce_access(email: str, ocp_username: str | None = None):
    """Check if user is allowed via group membership or email allowlist."""
    if _allowed_groups and ocp_username:
        user_groups = _get_user_groups(ocp_username, email)
        all_configured_groups = _allowed_groups | _admin_groups | _operator_groups
        if user_groups & all_configured_groups:
            return
        if _allowed_users and email.lower() in _allowed_users:
            return
        logger.warning(
            "Access denied for %s — groups: %s, allowed: %s",
            ocp_username,
            ", ".join(sorted(user_groups)) or "(none)",
            ", ".join(sorted(all_configured_groups)),
        )
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: user '{ocp_username}' is not in an allowed group.",
        )
    if _allowed_users and email.lower() not in _allowed_users:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied for user {email}",
        )


def get_current_user(request: Request, db: Session = Depends(get_db)):
    from app.models.user import User

    # SSO mode: use OAuth proxy headers
    if config.auth.oauth_enabled:
        user_info = _get_user_from_oauth_headers(request)
        if user_info:
            ocp_username = user_info.get("user")
            _enforce_access(user_info["email"], ocp_username)
            resolved_role = _resolve_role(user_info["email"], ocp_username)
            user = _upsert_sso_user(
                user_info["email"],
                ocp_username,
                db,
                resolved_role=resolved_role,
            )
            return user

    # Try API key (trk_ prefix)
    api_key_user = _get_user_from_api_key(request, db)
    if api_key_user:
        _enforce_access(api_key_user.email)
        return api_key_user

    # Try JWT token (works in both dev and SSO mode)
    user_info = _get_user_from_jwt(request)
    if user_info:
        email = user_info.get("email") or user_info.get("sub")
        if email:
            _enforce_access(email)
            user = db.query(User).filter_by(email=email).first()
            if user:
                return user

    # Dev mode: auto-authenticate as the default admin user
    if not config.auth.oauth_enabled:
        return _get_or_create_dev_user(db)

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
