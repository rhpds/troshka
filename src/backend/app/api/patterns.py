"""
Pattern API — create, share, deploy, and manage reusable VM topology patterns.
"""
import copy
import logging
import random
import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.pattern import Pattern, PatternShare
from app.models.project import Project
from app.models.user import User
from app.schemas.pattern import (
    PatternBulkDeployRequest,
    PatternCreate,
    PatternDeployRequest,
    PatternShareRequest,
    PatternUpdate,
)
from app.services.pattern_service import get_capture_progress

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/patterns", tags=["patterns"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_mac() -> str:
    """Generate a random MAC address with the QEMU prefix 52:54:00."""
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )


def _remap_topology(topology: dict) -> dict:
    """Clone a topology dict with all-new UUIDs, MACs, and controller IDs.

    - Every node gets a new UUID-based ``id``
    - Edges are updated to reference the new node IDs and handle IDs
    - NIC MAC addresses are regenerated
    - NIC ids and diskController ids are regenerated
    - Network CIDRs, DHCP ranges, DNS domains are preserved
    - BMC network credentials (bmcPassword) are preserved for pattern stability
    """
    topo = copy.deepcopy(topology)

    id_map: dict[str, str] = {}
    handle_id_map: dict[str, str] = {}

    for node in topo.get("nodes", []):
        old_id = node["id"]
        new_id = str(uuid.uuid4())
        id_map[old_id] = new_id
        node["id"] = new_id

        data = node.get("data", {})

        for nic in data.get("nics", []):
            old_nic_id = nic["id"]
            new_nic_id = f"nic-{uuid.uuid4()}"
            handle_id_map[old_nic_id] = new_nic_id
            nic["id"] = new_nic_id
            # Preserve MACs — CoreOS/ignition bakes network config with specific MACs

        for dc in data.get("diskControllers", []):
            old_dc_id = dc["id"]
            new_dc_id = f"dp-{uuid.uuid4()}"
            handle_id_map[old_dc_id] = new_dc_id
            dc["id"] = new_dc_id

    for node in topo.get("nodes", []):
        data = node.get("data", {})
        if "bootDevices" in data:
            data["bootDevices"] = [
                id_map.get(d, d) if d != "network" else d
                for d in data["bootDevices"]
            ]

    def _remap_handle(handle: str) -> str:
        if not handle:
            return handle
        for old_id, new_id in handle_id_map.items():
            if old_id in handle:
                handle = handle.replace(old_id, new_id)
        return handle

    for edge in topo.get("edges", []):
        if edge.get("source") in id_map:
            edge["source"] = id_map[edge["source"]]
        if edge.get("target") in id_map:
            edge["target"] = id_map[edge["target"]]
        if edge.get("sourceHandle"):
            edge["sourceHandle"] = _remap_handle(edge["sourceHandle"])
        if edge.get("targetHandle"):
            edge["targetHandle"] = _remap_handle(edge["targetHandle"])
        if "id" in edge:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            sh = edge.get("sourceHandle", "")
            th = edge.get("targetHandle", "")
            edge["id"] = f"xy-edge__{src}{sh}-{tgt}{th}"

    topo["startOrder"] = [
        {**entry, "vmId": id_map.get(entry["vmId"], entry["vmId"]),
         "waitForVm": id_map.get(entry["waitForVm"], entry["waitForVm"]) if entry.get("waitForVm") else None}
        for entry in topo.get("startOrder", [])
    ]

    eip_id_map = {}
    new_eips = []
    for entry in topo.get("externalIps", []):
        new_id = f"eip-{uuid.uuid4().hex[:12]}"
        eip_id_map[entry["id"]] = new_id
        new_eips.append({"id": new_id, "name": entry.get("name", ""), "ip": ""})
    topo["externalIps"] = new_eips
    for node in topo.get("nodes", []):
        for pf in node.get("data", {}).get("portForwards", []):
            old_eip_id = pf.get("extIpId", "")
            if old_eip_id in eip_id_map:
                pf["extIpId"] = eip_id_map[old_eip_id]

    topo["hiddenNodeIds"] = [
        id_map.get(nid, nid) for nid in topo.get("hiddenNodeIds", [])
    ]

    return topo


def _pattern_to_list_dict(p: Pattern) -> dict:
    """Serialize a Pattern for list responses (lightweight)."""
    nodes = (p.topology or {}).get("nodes", [])
    vm_count = sum(1 for n in nodes if n.get("type") == "vmNode")
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "owner_id": p.owner_id,
        "visibility": p.visibility,
        "state": p.state,
        "capture_progress": get_capture_progress(p.id) if p.state == "capturing" else None,
        "total_size_bytes": p.total_size_bytes,
        "tags": p.tags,
        "created_at": p.created_at,
        "disk_count": len(p.disks),
        "vm_count": vm_count,
    }


def _pattern_to_detail_dict(p: Pattern) -> dict:
    """Serialize a Pattern for detail responses (full)."""
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "owner_id": p.owner_id,
        "visibility": p.visibility,
        "source_project_id": p.source_project_id,
        "topology": p.topology,
        "state": p.state,
        "capture_progress": get_capture_progress(p.id) if p.state == "capturing" else None,
        "total_size_bytes": p.total_size_bytes,
        "tags": p.tags,
        "created_at": p.created_at,
        "disks": [
            {
                "id": d.id,
                "source_disk_id": d.source_disk_id,
                "source_vm_id": d.source_vm_id,
                "s3_key": d.s3_key,
                "format": d.format,
                "size_bytes": d.size_bytes,
                "virtual_size_bytes": d.virtual_size_bytes,
                "checksum_sha256": d.checksum_sha256,
                "state": d.state,
            }
            for d in p.disks
        ],
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("/", status_code=201)
def create_pattern(
    body: PatternCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a pattern — either from a project (source_project_id) or from a
    raw topology+disk_mappings payload."""

    existing = db.query(Pattern).filter_by(owner_id=user.id, name=body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"You already have a pattern named \"{body.name}\"")

    if body.source_project_id:
        # Capture from an existing project
        project = db.query(Project).filter_by(id=body.source_project_id, owner_id=user.id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Source project not found")
        if project.state not in ("active", "stopped"):
            raise HTTPException(status_code=400, detail="Project must be deployed (active or stopped) to save as pattern")
        topology = project.topology or {}
        state = "capturing"
    elif body.topology:
        topology = body.topology
        state = "available"
    else:
        raise HTTPException(status_code=400, detail="Provide source_project_id or topology")

    pattern = Pattern(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        visibility=body.visibility,
        source_project_id=body.source_project_id,
        topology=topology,
        state=state,
        tags=body.tags,
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)

    # If capturing from project, kick off async disk capture
    if body.source_project_id:
        from app.services.pattern_service import capture_pattern_disks
        threading.Thread(
            target=capture_pattern_disks,
            args=(pattern.id, body.source_project_id, body.restart_after),
            daemon=True,
        ).start()

    return _pattern_to_detail_dict(pattern)


@router.get("/")
def list_patterns(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List patterns visible to the current user:
    - own patterns
    - patterns shared with them
    - public patterns
    Admin users see everything.
    """
    if user.role == "admin":
        patterns = db.query(Pattern).order_by(Pattern.created_at.desc()).all()
    else:
        shared_ids = [
            s.pattern_id
            for s in db.query(PatternShare.pattern_id).filter_by(user_id=user.id).all()
        ]
        patterns = (
            db.query(Pattern)
            .filter(
                or_(
                    Pattern.owner_id == user.id,
                    Pattern.id.in_(shared_ids) if shared_ids else False,
                    Pattern.visibility == "public",
                )
            )
            .order_by(Pattern.created_at.desc())
            .all()
        )

    return [_pattern_to_list_dict(p) for p in patterns]


@router.get("/{pattern_id}")
def get_pattern(
    pattern_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    # Access check: owner, admin, shared, or public
    if pattern.owner_id != user.id and user.role != "admin" and pattern.visibility != "public":
        shared = db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=user.id).first()
        if not shared:
            raise HTTPException(status_code=404, detail="Pattern not found")

    return _pattern_to_detail_dict(pattern)


@router.patch("/{pattern_id}")
def update_pattern(
    pattern_id: str,
    body: PatternUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    if body.name is not None:
        pattern.name = body.name
    if body.description is not None:
        pattern.description = body.description
    if body.visibility is not None:
        pattern.visibility = body.visibility
    if body.tags is not None:
        pattern.tags = body.tags

    db.commit()
    db.refresh(pattern)
    return _pattern_to_detail_dict(pattern)


@router.delete("/{pattern_id}", status_code=204)
def delete_pattern(
    pattern_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    # S3 cleanup for captured disks
    for disk in pattern.disks:
        try:
            from app.services import s3_storage
            s3_storage.delete_file(disk.s3_key)
        except Exception:
            logger.warning("Failed to delete S3 object %s for pattern disk", disk.s3_key)

    db.delete(pattern)
    db.commit()

    # Clean pattern cache on all hosts in background
    import threading
    def _clean_pattern_cache(pid: str):
        from app.core.database import SessionLocal
        from app.models.host import Host
        from app.services.troshkad_client import start_job, wait_for_job, TroshkadError
        s = SessionLocal()
        try:
            for host in s.query(Host).filter(Host.agent_status == "connected").all():
                cache_path = f"/var/lib/troshka/cache/patterns/{pid}"
                shared_path = f"/var/lib/troshka/shared/cache/patterns/{pid}"
                try:
                    job_id = start_job(host, "/files/remove", {"paths": [cache_path, shared_path]})
                    wait_for_job(host, job_id, timeout=15)
                except TroshkadError:
                    pass
        finally:
            s.close()
    threading.Thread(target=_clean_pattern_cache, args=(pattern_id,), daemon=True).start()


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


@router.post("/{pattern_id}/share")
def share_pattern(
    pattern_id: str,
    body: PatternShareRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can share")

    target_user = db.query(User).filter_by(email=body.user_email).first()
    if not target_user:
        raise HTTPException(status_code=404, detail=f"User {body.user_email} not found")
    if target_user.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot share with yourself")

    existing = db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=target_user.id).first()
    if not existing:
        db.add(PatternShare(pattern_id=pattern_id, user_id=target_user.id))
        db.commit()

    return {"shared_with": body.user_email}


@router.delete("/{pattern_id}/share/{user_email}")
def revoke_share(
    pattern_id: str,
    user_email: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can revoke sharing")

    target_user = db.query(User).filter_by(email=user_email).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    share = db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=target_user.id).first()
    if share:
        db.delete(share)
        db.commit()

    return {"unshared": user_email}


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


@router.get("/{pattern_id}/progress")
def pattern_progress(
    pattern_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id and user.role != "admin" and pattern.visibility != "public":
        shared = db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=user.id).first()
        if not shared:
            raise HTTPException(status_code=404, detail="Pattern not found")

    progress = get_capture_progress(pattern_id)
    if progress is None:
        return {"pattern_id": pattern_id, "state": pattern.state, "progress": None}

    return {"pattern_id": pattern_id, "state": pattern.state, "progress": progress}


# ---------------------------------------------------------------------------
# Deploy — create a single project from a pattern
# ---------------------------------------------------------------------------


@router.post("/{pattern_id}/deploy", status_code=201)
def deploy_pattern(
    pattern_id: str,
    body: PatternDeployRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new project in 'draft' state from a pattern.

    Clones the topology with all-new UUIDs, regenerated MACs, and fresh
    disk-controller IDs while preserving network configuration.
    """
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    # Access check
    if pattern.owner_id != user.id and user.role != "admin" and pattern.visibility != "public":
        shared = db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=user.id).first()
        if not shared:
            raise HTTPException(status_code=404, detail="Pattern not found")

    project_name = body.name or f"{pattern.name} (deploy)"
    existing = db.query(Project).filter_by(owner_id=user.id, name=project_name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"You already have a project named \"{project_name}\"")

    new_topology = _remap_topology(pattern.topology)

    project = Project(
        name=project_name,
        description=body.description or pattern.description,
        owner_id=user.id,
        topology=new_topology,
        state="draft",
    )
    if body.guid:
        project.guid = body.guid
    if body.domain:
        project.domain = body.domain
    if body.dns_provider_id:
        project.dns_provider_id = body.dns_provider_id
    db.add(project)
    db.commit()
    db.refresh(project)

    if body.auto_deploy:
        from app.services.deploy_service import deploy_project_async
        project.state = "deploying"
        db.commit()
        threading.Thread(target=deploy_project_async, args=(project.id, body.auto_start), daemon=True, name=f"deploy-{project.id[:8]}").start()

    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "owner_id": project.owner_id,
        "state": project.state,
        "topology": project.topology,
        "created_at": project.created_at,
    }


# ---------------------------------------------------------------------------
# Bulk Deploy — create N projects from a pattern
# ---------------------------------------------------------------------------


def _bulk_deploy_projects(project_ids: list[str]):
    from app.core.database import SessionLocal
    from app.services.placement import place_project, calculate_project_requirements
    from app.services.deploy_service import deploy_project_async

    ready_ids = []
    s = SessionLocal()
    try:
        for project_id in project_ids:
            project = s.query(Project).filter_by(id=project_id).first()
            if not project or project.state != "draft" or not project.topology:
                continue
            reqs = calculate_project_requirements(project.topology)
            if reqs["vm_count"] == 0:
                continue
            result = place_project(s, project)
            if "error" in result:
                logger.warning("Bulk deploy: placement failed for %s: %s", project_id[:8], result["error"])
                project.state = "error"
                project.deploy_error = result["error"]
                continue
            project.vni_map = result.get("vni_map")
            project.state = "deploying"
            ready_ids.append(project_id)
        s.commit()
    except Exception:
        logger.exception("Bulk deploy: placement phase failed")
        return
    finally:
        s.close()

    for pid in ready_ids:
        threading.Thread(target=deploy_project_async, args=(pid,), daemon=True).start()


@router.post("/{pattern_id}/bulk-deploy", status_code=201)
def bulk_deploy_pattern(
    pattern_id: str,
    body: PatternBulkDeployRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create N projects from a pattern.

    ``name_template`` may contain ``{n}`` which is replaced with a zero-padded
    3-digit index (001, 002, ...).  If ``auto_deploy`` is true, each project
    is placed and deployed in a background thread after creation.
    """
    if body.count < 1 or body.count > 500:
        raise HTTPException(status_code=400, detail="count must be between 1 and 500")

    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")

    # Access check
    if pattern.owner_id != user.id and user.role != "admin" and pattern.visibility != "public":
        shared = db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=user.id).first()
        if not shared:
            raise HTTPException(status_code=404, detail="Pattern not found")

    projects = []
    for i in range(1, body.count + 1):
        name = body.name_template.replace("{n}", f"{i:03d}")
        new_topology = _remap_topology(pattern.topology)
        project = Project(
            name=name,
            description=pattern.description,
            owner_id=user.id,
            topology=new_topology,
            state="draft",
        )
        if body.guid_template:
            project.guid = body.guid_template.replace("{n}", f"{i:03d}")
        if body.domain:
            project.domain = body.domain
        if body.dns_provider_id:
            project.dns_provider_id = body.dns_provider_id
        db.add(project)
        projects.append(project)

    db.commit()
    for p in projects:
        db.refresh(p)

    if body.auto_deploy:
        project_ids = [p.id for p in projects]
        import threading
        threading.Thread(target=_bulk_deploy_projects, args=(project_ids,), daemon=True).start()

    return {
        "pattern_id": pattern_id,
        "count": len(projects),
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "state": p.state,
                "created_at": p.created_at,
            }
            for p in projects
        ],
    }
