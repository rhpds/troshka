from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.dns_provider import DnsProvider
from app.models.user import User
from app.schemas.dns_provider import DnsProviderCreate, DnsProviderResponse, DnsProviderUpdate

router = APIRouter(prefix="/dns-providers", tags=["dns-providers"])


@router.get("/", response_model=list[DnsProviderResponse])
def list_dns_providers(user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    return db.query(DnsProvider).order_by(DnsProvider.name).all()


@router.get("/{provider_id}", response_model=DnsProviderResponse)
def get_dns_provider(provider_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, "DNS provider not found")
    return provider


@router.post("/", response_model=DnsProviderResponse, status_code=201)
def create_dns_provider(body: DnsProviderCreate, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    existing = db.query(DnsProvider).filter_by(name=body.name).first()
    if existing:
        raise HTTPException(409, "DNS provider with this name already exists")
    provider = DnsProvider(name=body.name, type=body.type, config=body.config)
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


@router.patch("/{provider_id}", response_model=DnsProviderResponse)
def update_dns_provider(provider_id: str, body: DnsProviderUpdate, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, "DNS provider not found")
    if body.name is not None:
        provider.name = body.name
    if body.config is not None:
        provider.config = body.config
    db.commit()
    db.refresh(provider)
    return provider


@router.delete("/{provider_id}", status_code=204)
def delete_dns_provider(provider_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, "DNS provider not found")
    db.delete(provider)
    db.commit()
