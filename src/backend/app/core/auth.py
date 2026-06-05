import datetime
import logging

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import config
from app.core.database import get_db

logger = logging.getLogger(__name__)

_group_cache: dict[str, dict] = {}
GROUP_CACHE_TTL = 60


def _parse_csv(value: str) -> set[str]:
    if not value:
        return set()
    return {v.strip().lower() for v in str(value).split(",") if v.strip()}


_admin_users = _parse_csv(getattr(config.auth, "admin_users", ""))
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
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=config.auth.jwt_expiry_hours),
    }
    return jwt.encode(payload, config.auth.jwt_secret, algorithm=config.auth.jwt_algorithm)


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, config.auth.jwt_secret, algorithms=[config.auth.jwt_algorithm])
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


def _upsert_sso_user(email: str, display_name: str | None, db: Session):
    from app.models.user import User

    user = db.query(User).filter_by(email=email).first()
    if user is None:
        user = User(
            email=email,
            display_name=display_name or email.split("@")[0],
            role=role_for_email(email),
            auth_source="sso",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("Created SSO user %s with role %s", email, user.role)
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


def get_current_user(request: Request, db: Session = Depends(get_db)):
    from app.models.user import User

    # SSO mode: use OAuth proxy headers
    if config.auth.oauth_enabled:
        user_info = _get_user_from_oauth_headers(request)
        if user_info:
            return _upsert_sso_user(
                user_info["email"],
                user_info.get("user"),
                db,
            )

    # Try API key (trk_ prefix)
    api_key_user = _get_user_from_api_key(request, db)
    if api_key_user:
        return api_key_user

    # Try JWT token (works in both dev and SSO mode)
    user_info = _get_user_from_jwt(request)
    if user_info:
        email = user_info.get("email") or user_info.get("sub")
        if email:
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
