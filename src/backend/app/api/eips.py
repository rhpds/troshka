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


@router.post("/projects/{project_id}/eips/sync")
def sync_project_eips(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Allocate/associate EIPs for a running project without redeploying VMs."""
    from app.models.project import Project
    from app.models.host import Host
    from app.services.eip_service import allocate_eip, associate_eip, sync_security_group_rules
    from app.services.deploy_service import _setup_networks_via_troshkad

    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state != "active":
        raise HTTPException(status_code=409, detail=f"Project must be active (currently {project.state})")
    if not project.host_id:
        raise HTTPException(status_code=409, detail="Project has no host assigned")

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.ip_address:
        raise HTTPException(status_code=503, detail="Host not reachable")

    provider = db.query(Provider).filter_by(id=project.provider_id).first() if project.provider_id else None
    if not provider and host.provider_id:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        raise HTTPException(status_code=409, detail="No provider configured for EIP allocation")

    topology = project.topology or {}
    external_ips = topology.get("externalIps", [])
    if not external_ips:
        return {"status": "no_eips", "message": "No external IPs in topology"}

    allocated = []
    for ext_ip in external_ips:
        canvas_id = ext_ip.get("id", "")
        existing = db.query(ElasticIp).filter_by(
            project_id=project_id, canvas_eip_id=canvas_id
        ).first()
        if existing:
            eip = existing
        else:
            eip = allocate_eip(db, provider, project_id, canvas_id)

        if eip.state != "associated":
            associate_eip(db, eip, host)

        ext_ip["ip"] = eip.public_ip
        ext_ip["_private_ip"] = eip.private_ip
        allocated.append({"name": ext_ip.get("name"), "ip": eip.public_ip})

    project.topology = topology
    db.commit()

    # Re-run network setup to apply DNAT rules for the new EIPs
    vni_map = project.vni_map or {}
    net_result = _setup_networks_via_troshkad(host, topology, vni_map, db, project_id)
    if net_result is not True:
        logger.error("EIP sync network setup failed: %s", net_result)

    # Sync SG rules
    gateway_node = next(
        (n for n in topology.get("nodes", [])
         if n.get("type") == "networkNode" and n.get("data", {}).get("subtype") == "gateway"
         and n.get("data", {}).get("gatewayMode") == "nat-portforward"),
        None,
    )
    if gateway_node:
        desired_sg = [
            {"project_id": project_id, "ext_port": int(pf["extPort"]), "protocol": "tcp"}
            for pf in gateway_node.get("data", {}).get("portForwards", [])
            if pf.get("extPort")
        ]
        sync_security_group_rules(db, provider, desired_sg)

    return {"status": "synced", "eips": allocated}


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
