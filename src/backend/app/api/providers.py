import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.provider import Provider
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/providers", tags=["providers"])


class ProviderCreate(BaseModel):
    name: str
    type: str
    default_region: str
    default_ami: str = ""
    access_key_id: str
    secret_access_key: str


class ProviderUpdate(BaseModel):
    name: str | None = None
    default_region: str | None = None
    default_ami: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    state: str | None = None


class ProviderResponse(BaseModel):
    id: str
    name: str
    type: str
    default_region: str | None
    default_ami: str | None
    state: str
    has_credentials: bool
    host_count: int
    created_at: str

    model_config = {"from_attributes": False}


@router.get("/", response_model=list[ProviderResponse])
def list_providers(user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    providers = db.query(Provider).order_by(Provider.name).all()
    return [
        ProviderResponse(
            id=p.id,
            name=p.name,
            type=p.type,
            default_region=p.default_region,
            default_ami=p.default_ami,
            state=p.state,
            has_credentials=bool(p.credentials),
            host_count=len(p.hosts),
            created_at=p.created_at.isoformat() if p.created_at else "",
        )
        for p in providers
    ]


@router.post("/", response_model=ProviderResponse, status_code=201)
def create_provider(body: ProviderCreate, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    existing = db.query(Provider).filter_by(name=body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Provider name already exists")

    provider = Provider(
        name=body.name,
        type=body.type,
        default_region=body.default_region,
        default_ami=body.default_ami or None,
        created_by=user.email,
    )
    provider.set_credentials({
        "access_key_id": body.access_key_id,
        "secret_access_key": body.secret_access_key,
    })
    db.add(provider)
    db.commit()
    db.refresh(provider)

    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        type=provider.type,
        default_region=provider.default_region,
        default_ami=provider.default_ami,
        state=provider.state,
        has_credentials=True,
        host_count=0,
        created_at=provider.created_at.isoformat() if provider.created_at else "",
    )


@router.patch("/{provider_id}", response_model=ProviderResponse)
def update_provider(provider_id: str, body: ProviderUpdate, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if body.name is not None:
        provider.name = body.name
    if body.default_region is not None:
        provider.default_region = body.default_region
    if body.default_ami is not None:
        provider.default_ami = body.default_ami
    if body.state is not None:
        provider.state = body.state

    if body.access_key_id or body.secret_access_key:
        creds = provider.get_credentials()
        if body.access_key_id:
            creds["access_key_id"] = body.access_key_id
        if body.secret_access_key:
            creds["secret_access_key"] = body.secret_access_key
        provider.set_credentials(creds)

    db.commit()
    db.refresh(provider)

    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        type=provider.type,
        default_region=provider.default_region,
        default_ami=provider.default_ami,
        state=provider.state,
        has_credentials=bool(provider.credentials),
        host_count=len(provider.hosts),
        created_at=provider.created_at.isoformat() if provider.created_at else "",
    )


@router.delete("/{provider_id}", status_code=204)
def delete_provider(provider_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.hosts:
        raise HTTPException(status_code=409, detail="Provider has hosts — remove them first")
    db.delete(provider)
    db.commit()


@router.get("/{provider_id}/discover-ami")
def list_available_amis(provider_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """List available RHEL 9 AMIs (both Access2/Gold and Hourly/Marketplace)."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    try:
        ec2 = boto3.client(
            "ec2",
            region_name=provider.default_region,
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )

        ami_types = {
            "access2": {"pattern": "RHEL-9.4*x86_64*Access2-GP3", "label": "RHEL Access2 (Gold Image / BYOS)"},
            "hourly": {"pattern": "RHEL-9.4*x86_64*Hourly2-GP3", "label": "RHEL Marketplace (Hourly)"},
        }

        results = []
        for ami_type, info in ami_types.items():
            response = ec2.describe_images(
                Owners=["309956199498"],
                Filters=[
                    {"Name": "name", "Values": [info["pattern"]]},
                    {"Name": "state", "Values": ["available"]},
                ],
            )
            images = sorted(response["Images"], key=lambda x: x["CreationDate"])
            if images:
                latest = images[-1]
                results.append({
                    "type": ami_type,
                    "label": info["label"],
                    "ami_id": latest["ImageId"],
                    "name": latest["Name"],
                    "created": latest["CreationDate"],
                })

        return {"region": provider.default_region, "amis": results}
    except Exception:
        logger.exception("AMI discovery failed for %s", provider.name)
        raise HTTPException(status_code=500, detail="AMI discovery failed. Check server logs.")


@router.post("/{provider_id}/set-ami")
def set_ami(provider_id: str, ami_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set the default AMI for a provider."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    provider.default_ami = ami_id
    db.commit()
    return {"ami_id": ami_id}


@router.post("/{provider_id}/test")
def test_provider(provider_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Test provider credentials by calling AWS STS."""
    import boto3

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    try:
        sts = boto3.client(
            "sts",
            region_name=provider.default_region,
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )
        identity = sts.get_caller_identity()
        return {
            "status": "ok",
            "account": identity["Account"],
            "arn": identity["Arn"],
        }
    except Exception:
        logger.exception("Provider test failed for %s", provider.name)
        raise HTTPException(status_code=400, detail="Credentials test failed")
