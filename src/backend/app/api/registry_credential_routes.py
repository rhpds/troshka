from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.encryption import encrypt
from app.core.database import get_db
from app.models.registry_credential import RegistryCredential
from app.models.user import User

router = APIRouter(prefix="/auth/registry-credentials", tags=["auth"])


@router.get("")
def list_registry_credentials(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    creds = (
        db.query(RegistryCredential)
        .filter(RegistryCredential.user_id == user.id)
        .order_by(RegistryCredential.name)
        .all()
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "registry": c.registry_url,
            "username": c.username,
            "created_at": str(c.created_at) if c.created_at else None,
        }
        for c in creds
    ]


@router.post("", status_code=201)
def create_registry_credential(
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    name = (body.get("name") or "").strip()
    registry = (body.get("registry") or "").strip()
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    if not all([name, registry, username, password]):
        raise HTTPException(
            status_code=400,
            detail="name, registry, username, and password are required",
        )
    cred = RegistryCredential(
        user_id=user.id,
        name=name,
        registry_url=registry,
        username=username,
        password=encrypt(password),
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return {"id": cred.id, "name": cred.name, "registry": cred.registry}


@router.put("/{cred_id}")
def update_registry_credential(
    cred_id: str,
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(RegistryCredential)
        .filter(
            RegistryCredential.id == cred_id,
            RegistryCredential.user_id == user.id,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    if "name" in body:
        cred.name = body["name"].strip()
    if "registry" in body:
        cred.registry_url = body["registry"].strip()
    if "username" in body:
        cred.username = body["username"].strip()
    if "password" in body and body["password"].strip():
        cred.password = encrypt(body["password"].strip())
    db.commit()
    return {"id": cred.id, "name": cred.name, "registry": cred.registry}


@router.delete("/{cred_id}", status_code=204)
def delete_registry_credential(
    cred_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(RegistryCredential)
        .filter(
            RegistryCredential.id == cred_id,
            RegistryCredential.user_id == user.id,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    db.delete(cred)
    db.commit()
