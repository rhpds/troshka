import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.api_key import ApiKey, generate_api_key, hash_key
from app.models.user import User

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str
    expires_days: int | None = None


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    is_active: bool
    last_used_at: datetime.datetime | None = None
    expires_at: datetime.datetime | None = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class ApiKeyCreated(ApiKeyResponse):
    key: str


@router.get("/", response_model=list[ApiKeyResponse])
def list_api_keys(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(ApiKey).filter_by(user_id=user.id).order_by(ApiKey.created_at.desc()).all()


@router.post("/", response_model=ApiKeyCreated, status_code=201)
def create_api_key(body: ApiKeyCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    raw_key = generate_api_key()

    expires_at = None
    if body.expires_days:
        expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=body.expires_days)

    api_key = ApiKey(
        user_id=user.id,
        name=body.name,
        key_hash=hash_key(raw_key),
        key_prefix=raw_key[:10],
        expires_at=expires_at,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    return ApiKeyCreated(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        is_active=api_key.is_active,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        created_at=api_key.created_at,
        key=raw_key,
    )


@router.delete("/{key_id}", status_code=204)
def revoke_api_key(key_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    api_key = db.query(ApiKey).filter_by(id=key_id, user_id=user.id).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    db.delete(api_key)
    db.commit()
