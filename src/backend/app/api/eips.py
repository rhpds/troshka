import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.models.elastic_ip import ElasticIp
from app.models.provider import Provider
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["eips"])


@router.delete("/projects/{project_id}/eips/{canvas_eip_id}", status_code=200)
def release_project_eip(
    project_id: str,
    canvas_eip_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Release a specific EIP from a project (when user removes it from canvas)."""
    from app.models.project import Project
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    eip = db.query(ElasticIp).filter_by(
        project_id=project_id, canvas_eip_id=canvas_eip_id
    ).first()
    if not eip:
        return {"status": "not_allocated"}

    from app.services.eip_service import release_eip
    release_eip(db, eip)
    return {"status": "released", "public_ip": eip.public_ip}


@router.get("/projects/{project_id}/eips")
def list_project_eips(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all EIPs allocated for a project."""
    from app.models.project import Project
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    eips = db.query(ElasticIp).filter_by(project_id=project_id).all()
    return [
        {
            "id": eip.id,
            "canvas_eip_id": eip.canvas_eip_id,
            "public_ip": eip.public_ip,
            "state": eip.state,
            "host_id": eip.host_id,
        }
        for eip in eips
    ]


@router.post("/providers/{provider_id}/gc")
def provider_gc(
    provider_id: str,
    dry_run: bool = False,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Run provider-level garbage collection on AWS resources."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type not in ("ec2",):
        raise HTTPException(status_code=400, detail="GC only supported for EC2 providers")

    from app.services.provider_gc_service import reconcile_provider
    return reconcile_provider(db, provider, dry_run=dry_run)
