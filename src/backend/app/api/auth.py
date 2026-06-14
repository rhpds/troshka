from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import create_jwt, get_current_user
from app.core.config import config
from app.core.database import get_db
from app.models.user import User, UserSshKey
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
        raise HTTPException(
            status_code=403, detail="Dev tokens disabled when SSO is enabled"
        )

    user = _get_or_create_dev_user(db)
    token = create_jwt(user_id=user.id, email=user.email, role=user.role)
    return LoginResponse(
        token=token,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
    )


@router.get("/dev-token/{role}", response_model=LoginResponse)
def dev_token_with_role(role: str, db: Session = Depends(get_db)):
    if config.auth.oauth_enabled:
        raise HTTPException(
            status_code=403, detail="Dev tokens disabled when SSO is enabled"
        )
    if role not in ("admin", "operator", "user"):
        raise HTTPException(
            status_code=400, detail="Role must be admin, operator, or user"
        )

    user = _get_or_create_dev_user(db, role=role)
    token = create_jwt(user_id=user.id, email=user.email, role=user.role)
    return LoginResponse(
        token=token,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
    )


@router.get("/me", response_model=UserIdentity)
def auth_me(user: User = Depends(get_current_user)):
    return UserIdentity.model_validate(user)


# ── SSH Keys ──

from pydantic import BaseModel


class SshKeyCreate(BaseModel):
    name: str
    public_key: str


@router.get("/ssh-keys")
def list_ssh_keys(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    keys = (
        db.query(UserSshKey)
        .filter_by(user_id=user.id)
        .order_by(UserSshKey.created_at)
        .all()
    )
    return [
        {
            "id": k.id,
            "name": k.name,
            "public_key": k.public_key,
            "created_at": str(k.created_at),
        }
        for k in keys
    ]


@router.post("/ssh-keys", status_code=201)
def add_ssh_key(
    body: SshKeyCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    import re

    pk = body.public_key.strip()
    if not re.match(
        r"^(ssh-(rsa|ed25519|dss|ecdsa-sha2-nistp(256|384|521))|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/=]+",
        pk,
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid SSH public key format. Must start with ssh-rsa, ssh-ed25519, etc.",
        )
    key = UserSshKey(user_id=user.id, name=body.name, public_key=pk)
    db.add(key)
    db.commit()
    db.refresh(key)
    return {"id": key.id, "name": key.name}


@router.delete("/ssh-keys/{key_id}", status_code=204)
def delete_ssh_key(
    key_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    key = db.query(UserSshKey).filter_by(id=key_id, user_id=user.id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")
    db.delete(key)
    db.commit()


# ── OCP Pull Secret ──


@router.get("/ocp-pull-secret")
def get_ocp_pull_secret(user: User = Depends(get_current_user)):
    if not user.ocp_pull_secret:
        return {"has_secret": False, "masked": ""}
    from app.core.encryption import decrypt

    raw = decrypt(user.ocp_pull_secret)
    masked = raw[:20] + "..." if len(raw) > 20 else raw
    return {"has_secret": True, "masked": masked}


@router.put("/ocp-pull-secret")
def set_ocp_pull_secret(
    body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    secret = body.get("pull_secret", "").strip()
    if not secret:
        raise HTTPException(status_code=400, detail="Pull secret is required")
    import json

    try:
        json.loads(secret)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Pull secret must be valid JSON")
    from app.core.encryption import encrypt

    user.ocp_pull_secret = encrypt(secret)
    db.commit()
    return {"status": "saved"}


@router.delete("/ocp-pull-secret", status_code=204)
def delete_ocp_pull_secret(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    user.ocp_pull_secret = None
    db.commit()
