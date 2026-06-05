from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.models.host import Host
from app.models.user import User
from app.schemas.host import HostCreate, HostResponse
from app.services.provisioner import provision_host, terminate_host, get_host_status

router = APIRouter(prefix="/hosts", tags=["hosts"])


class ProvisionRequest(BaseModel):
    instance_type: str | None = None
    ami_id: str | None = None


@router.get("/", response_model=list[HostResponse])
def list_hosts(user: User = Depends(require_role("operator")), db: Session = Depends(get_db)):
    return db.query(Host).all()


@router.post("/", response_model=HostResponse, status_code=201)
def add_host(body: ProvisionRequest, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Provision a new EC2 host and add it to the pool."""
    try:
        result = provision_host(
            instance_type=body.instance_type,
            ami_id=body.ami_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to provision host: {e}")

    host = Host(
        id=result["host_id"],
        instance_id=result["instance_id"],
        instance_type=result["instance_type"],
        region=getattr(getattr(__import__("app.core.config", fromlist=["config"]), "config"), "aws").default_region,
        state="active",
        host_type="shared",
        total_vcpus=result["total_vcpus"],
        total_ram_mb=result["total_ram_mb"],
        ip_address=result["public_ip"],
        agent_status="disconnected",
    )
    db.add(host)
    db.commit()
    db.refresh(host)
    return host


@router.get("/{host_id}", response_model=HostResponse)
def get_host(host_id: str, user: User = Depends(require_role("operator")), db: Session = Depends(get_db)):
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


@router.delete("/{host_id}", status_code=204)
def remove_host(host_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Terminate the EC2 instance and remove the host from the pool."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if host.used_vcpus > 0:
        raise HTTPException(status_code=409, detail="Host has active projects — drain first")

    if host.instance_id:
        try:
            terminate_host(host.instance_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to terminate: {e}")

    db.delete(host)
    db.commit()
