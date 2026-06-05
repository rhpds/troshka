from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import create_jwt, get_current_user, role_for_email
from app.core.config import config
from app.core.database import get_db
from app.models.user import User
from app.schemas.auth import LoginResponse, UserIdentity

router = APIRouter(prefix="/auth", tags=["auth"])


def _get_or_create_dev_user(db: Session, role: str = "admin") -> User:
    email = "local-dev@troshka"
    user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(
            email=email,
            display_name="local-dev",
            role=role,
            auth_source="dev",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif user.role != role:
        user.role = role
        db.commit()
        db.refresh(user)

    return user


@router.get("/config")
def auth_config():
    return {
        "oauth_enabled": bool(config.auth.oauth_enabled),
        "dev_mode": not bool(config.auth.oauth_enabled),
    }


@router.get("/dev-token", response_model=LoginResponse)
def dev_token(db: Session = Depends(get_db)):
    if config.auth.oauth_enabled:
        raise HTTPException(status_code=403, detail="Dev tokens disabled when SSO is enabled")

    user = _get_or_create_dev_user(db)
    token = create_jwt(user_id=user.id, email=user.email, role=user.role)
    return LoginResponse(token=token, user_id=user.id, email=user.email, display_name=user.display_name, role=user.role)


@router.get("/dev-token/{role}", response_model=LoginResponse)
def dev_token_with_role(role: str, db: Session = Depends(get_db)):
    if config.auth.oauth_enabled:
        raise HTTPException(status_code=403, detail="Dev tokens disabled when SSO is enabled")
    if role not in ("admin", "operator", "user"):
        raise HTTPException(status_code=400, detail="Role must be admin, operator, or user")

    user = _get_or_create_dev_user(db, role=role)
    token = create_jwt(user_id=user.id, email=user.email, role=user.role)
    return LoginResponse(token=token, user_id=user.id, email=user.email, display_name=user.display_name, role=user.role)


@router.get("/me", response_model=UserIdentity)
def auth_me(user: User = Depends(get_current_user)):
    return UserIdentity.model_validate(user)
