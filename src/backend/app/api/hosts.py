import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.config import config
from app.core.database import get_db
from app.models.host import Host
from app.models.user import User
from app.schemas.host import HostResponse
from app.services.provisioner import provision_host, terminate_host

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hosts", tags=["hosts"])


class ProvisionRequest(BaseModel):
    instance_type: str | None = None
    region: str | None = None
    ami_id: str | None = None


@router.get("/", response_model=list[HostResponse])
def list_hosts(
    region: str | None = None,
    user: User = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    query = db.query(Host)
    if region:
        query = query.filter(Host.region == region)
    return query.order_by(Host.region, Host.created_at).all()


@router.get("/summary")
def host_summary(user: User = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Summary of host pool by region."""
    hosts = db.query(Host).all()
    regions: dict[str, dict] = {}
    for h in hosts:
        r = h.region or "unknown"
        if r not in regions:
            regions[r] = {"region": r, "total_hosts": 0, "active_hosts": 0, "total_vcpus": 0, "used_vcpus": 0, "total_ram_mb": 0, "used_ram_mb": 0}
        regions[r]["total_hosts"] += 1
        if h.state == "active":
            regions[r]["active_hosts"] += 1
        regions[r]["total_vcpus"] += h.total_vcpus
        regions[r]["used_vcpus"] += h.used_vcpus
        regions[r]["total_ram_mb"] += h.total_ram_mb
        regions[r]["used_ram_mb"] += h.used_ram_mb
    return list(regions.values())


@router.post("/", response_model=HostResponse, status_code=201)
def add_host(body: ProvisionRequest, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Provision a new EC2 host and add it to the pool."""
    region = body.region or config.aws.default_region
    try:
        result = provision_host(
            instance_type=body.instance_type,
            ami_id=body.ami_id,
        )
    except Exception as e:
        logger.exception("Failed to provision host: %s", e)
        raise HTTPException(status_code=500, detail="Failed to provision host. Check server logs.")

    host = Host(
        id=result["host_id"],
        instance_id=result["instance_id"],
        instance_type=result["instance_type"],
        region=region,
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
            logger.exception("Failed to terminate host %s: %s", host_id, e)
            raise HTTPException(status_code=500, detail="Failed to terminate host. Check server logs.")

    db.delete(host)
    db.commit()
