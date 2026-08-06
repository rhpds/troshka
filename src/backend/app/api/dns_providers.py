from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.dns_provider import DnsProvider
from app.models.user import User
from app.schemas.dns_provider import (
    DnsProviderCreate,
    DnsProviderResponse,
    DnsProviderUpdate,
)

router = APIRouter(prefix="/dns-providers", tags=["dns-providers"])

_DNS_PROVIDER_NOT_FOUND = "DNS provider not found"

AdminUser = Annotated[User, Depends(require_role("admin"))]
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/", response_model=list[DnsProviderResponse])
def list_dns_providers(user: AdminUser, db: DbSession):
    return db.query(DnsProvider).order_by(DnsProvider.name).all()


@router.get(
    "/{provider_id}",
    response_model=DnsProviderResponse,
    responses={404: {"description": "DNS provider not found"}},
)
def get_dns_provider(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, _DNS_PROVIDER_NOT_FOUND)
    return provider


@router.post(
    "/",
    response_model=DnsProviderResponse,
    status_code=201,
    responses={409: {"description": "DNS provider with this name already exists"}},
)
def create_dns_provider(
    body: DnsProviderCreate,
    user: AdminUser,
    db: DbSession,
):
    existing = db.query(DnsProvider).filter_by(name=body.name).first()
    if existing:
        raise HTTPException(409, "DNS provider with this name already exists")
    provider = DnsProvider(name=body.name, type=body.type, config=body.config)
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


@router.patch(
    "/{provider_id}",
    response_model=DnsProviderResponse,
    responses={404: {"description": "DNS provider not found"}},
)
def update_dns_provider(
    provider_id: str,
    body: DnsProviderUpdate,
    user: AdminUser,
    db: DbSession,
):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, _DNS_PROVIDER_NOT_FOUND)
    if body.name is not None:
        provider.name = body.name
    if body.config is not None:
        provider.config = body.config
    db.commit()
    db.refresh(provider)
    return provider


@router.delete(
    "/{provider_id}",
    status_code=204,
    responses={404: {"description": "DNS provider not found"}},
)
def delete_dns_provider(
    provider_id: str,
    user: AdminUser,
    db: DbSession,
):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, _DNS_PROVIDER_NOT_FOUND)
    db.delete(provider)
    db.commit()
