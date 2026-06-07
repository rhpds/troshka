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
    - Edges are updated to reference the new node IDs
    - NIC MAC addresses are regenerated
    - NIC ids and diskController ids are regenerated
    - Network CIDRs, DHCP ranges, DNS domains are preserved
    """
    topo = copy.deepcopy(topology)

    # Build old-id -> new-id mapping for nodes
    id_map: dict[str, str] = {}
    for node in topo.get("nodes", []):
        old_id = node["id"]
        new_id = str(uuid.uuid4())
        id_map[old_id] = new_id
        node["id"] = new_id

        data = node.get("data", {})

        # Regenerate NIC IDs and MAC addresses
        for nic in data.get("nics", []):
            nic["id"] = str(uuid.uuid4())
            nic["mac"] = _generate_mac()

        # Regenerate disk controller IDs
        for dc in data.get("diskControllers", []):
            dc["id"] = str(uuid.uuid4())

    # Remap edges
    for edge in topo.get("edges", []):
        if edge.get("source") in id_map:
            edge["source"] = id_map[edge["source"]]
        if edge.get("target") in id_map:
            edge["target"] = id_map[edge["target"]]
        # Regenerate edge id if present
        if "id" in edge:
            edge["id"] = str(uuid.uuid4())

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

    if body.source_project_id:
        # Capture from an existing project
        project = db.query(Project).filter_by(id=body.source_project_id, owner_id=user.id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Source project not found")
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
            args=(pattern.id, body.source_project_id),
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

    new_topology = _remap_topology(pattern.topology)

    project = Project(
        name=body.name or f"{pattern.name} (deploy)",
        description=body.description or pattern.description,
        owner_id=user.id,
        topology=new_topology,
        state="draft",
    )
    db.add(project)
    db.commit()
    db.refresh(project)

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


@router.post("/{pattern_id}/bulk-deploy", status_code=201)
def bulk_deploy_pattern(
    pattern_id: str,
    body: PatternBulkDeployRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create N projects from a pattern.

    ``name_template`` may contain ``{n}`` which is replaced with a zero-padded
    3-digit index (001, 002, ...).  ``auto_deploy`` is accepted but currently
    only creates drafts.
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
        db.add(project)
        projects.append(project)

    db.commit()
    for p in projects:
        db.refresh(p)

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
