"""
Pattern API — create, share, deploy, and manage reusable VM topology patterns.
"""

import copy
import datetime
import logging
import random
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import false as sa_false
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.logging_utils import sanitize_log
from app.models.pattern import Pattern, PatternDisk, PatternShare
from app.models.pattern_location import PatternLocation
from app.models.project import Project
from app.models.provider import Provider
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

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]

_PATTERN_NOT_FOUND = "Pattern not found"


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


def _remap_node_ids(nodes: list, id_map: dict, handle_id_map: dict) -> None:
    """Remap node IDs, NIC IDs, and disk controller IDs in-place."""
    for node in nodes:
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

        for dc in data.get("diskControllers", []):
            old_dc_id = dc["id"]
            new_dc_id = f"dp-{uuid.uuid4()}"
            handle_id_map[old_dc_id] = new_dc_id
            dc["id"] = new_dc_id


def _remap_boot_devices(nodes: list, id_map: dict) -> None:
    """Remap bootDevices references in-place."""
    for node in nodes:
        data = node.get("data", {})
        if "bootDevices" not in data:
            continue
        data["bootDevices"] = [
            id_map.get(d, d) if d != "network" else d for d in data["bootDevices"]
        ]


def _remap_handle(handle: str, handle_id_map: dict) -> str:
    """Replace old handle IDs with new ones."""
    if not handle:
        return handle
    for old_id, new_id in handle_id_map.items():
        if old_id in handle:
            handle = handle.replace(old_id, new_id)
    return handle


def _remap_edges(edges: list, id_map: dict, handle_id_map: dict) -> None:
    """Remap edge source/target and handles in-place."""
    for edge in edges:
        if edge.get("source") in id_map:
            edge["source"] = id_map[edge["source"]]
        if edge.get("target") in id_map:
            edge["target"] = id_map[edge["target"]]
        if edge.get("sourceHandle"):
            edge["sourceHandle"] = _remap_handle(edge["sourceHandle"], handle_id_map)
        if edge.get("targetHandle"):
            edge["targetHandle"] = _remap_handle(edge["targetHandle"], handle_id_map)
        if "id" in edge:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            sh = edge.get("sourceHandle", "")
            th = edge.get("targetHandle", "")
            edge["id"] = f"xy-edge__{src}{sh}-{tgt}{th}"


def _remap_start_order(start_order: list, id_map: dict) -> list:
    """Return a new start order list with remapped VM IDs."""
    return [
        {
            **entry,
            "vmId": id_map.get(entry["vmId"], entry["vmId"]),
            "waitForVm": (
                id_map.get(entry["waitForVm"], entry["waitForVm"])
                if entry.get("waitForVm")
                else None
            ),
        }
        for entry in start_order
    ]


def _remap_external_ips(topo: dict, nodes: list) -> None:
    """Remap external IPs and port forwards in-place."""
    eip_id_map = {}
    new_eips = []
    for entry in topo.get("externalIps", []):
        new_id = f"eip-{uuid.uuid4().hex[:12]}"
        eip_id_map[entry["id"]] = new_id
        new_eips.append({"id": new_id, "name": entry.get("name", ""), "ip": ""})
    topo["externalIps"] = new_eips

    for node in nodes:
        for pf in node.get("data", {}).get("portForwards", []):
            old_eip_id = pf.get("extIpId", "")
            if old_eip_id in eip_id_map:
                pf["extIpId"] = eip_id_map[old_eip_id]


def _clear_external_endpoints(nodes: list) -> None:
    """Clear externalEndpoints from all nodes in-place."""
    for node in nodes:
        if node.get("data", {}).get("externalEndpoints"):
            node["data"]["externalEndpoints"] = []


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

    nodes = topo.get("nodes", [])
    edges = topo.get("edges", [])

    _remap_node_ids(nodes, id_map, handle_id_map)
    _remap_boot_devices(nodes, id_map)
    _remap_edges(edges, id_map, handle_id_map)

    topo["startOrder"] = _remap_start_order(topo.get("startOrder", []), id_map)
    _remap_external_ips(topo, nodes)

    topo["hiddenNodeIds"] = [
        id_map.get(nid, nid) for nid in topo.get("hiddenNodeIds", [])
    ]

    _clear_external_endpoints(nodes)

    return topo


def _compute_sync_status(p: Pattern, db: Session) -> tuple[str | None, list[dict]]:
    """Compute aggregate sync status and per-provider location list."""
    if not p.disks:
        return None, []

    all_locs = (
        db.query(PatternLocation)
        .filter(PatternLocation.pattern_disk_id.in_([d.id for d in p.disks]))
        .all()
    )
    if not all_locs:
        return None, []

    provider_ids = {loc.provider_id for loc in all_locs if loc.provider_id is not None}
    providers = {
        prov.id: prov.name
        for prov in db.query(Provider).filter(Provider.id.in_(provider_ids)).all()
    }

    provider_states: dict[str, dict] = {}
    for loc in all_locs:
        pid = loc.provider_id
        if pid is None:
            continue
        if pid not in provider_states:
            provider_states[pid] = {
                "provider_id": pid,
                "provider_name": providers.get(pid),
                "state": "synced",
                "synced_at": loc.synced_at,
            }
        if loc.state != "synced":
            provider_states[pid]["state"] = loc.state

    kubevirt_providers = (
        db.query(Provider)
        .filter(Provider.type == "kubevirt", Provider.state == "active")
        .count()
    )
    synced_count = sum(1 for ps in provider_states.values() if ps["state"] == "synced")
    if kubevirt_providers > 0 and synced_count >= kubevirt_providers:
        sync_status = "synced"
    elif synced_count > 0:
        sync_status = f"partial {synced_count}/{kubevirt_providers}"
    else:
        sync_status = "local"

    return sync_status, list(provider_states.values())


def _pattern_to_list_dict(p: Pattern, db: Session | None = None) -> dict:
    """Serialize a Pattern for list responses (lightweight)."""
    nodes = (p.topology or {}).get("nodes", [])
    vms = [n for n in nodes if n.get("type") == "vmNode"]
    rhcos_vms = [vm for vm in vms if vm.get("data", {}).get("os") == "rhcos"]
    is_ocp = len(rhcos_vms) > 0
    is_sno = len(rhcos_vms) == 1
    total_vcpus = 0
    total_ram_gb = 0
    total_disk_gb = 0
    for vm in vms:
        data = vm.get("data", {})
        total_vcpus += data.get("vcpus", 2)
        total_ram_gb += data.get("ram", 4)
    for n in nodes:
        if n.get("type") == "storageNode":
            data = n.get("data", {})
            if data.get("format") != "iso":
                total_disk_gb += data.get("size", 0)

    sync_status = None
    if db and p.source_provider_id:
        sync_status, _ = _compute_sync_status(p, db)

    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "owner_id": p.owner_id,
        "visibility": p.visibility,
        "state": p.state,
        "capture_progress": (
            get_capture_progress(p.id) if p.state == "capturing" else None
        ),
        "total_size_bytes": p.total_size_bytes,
        "tags": p.tags,
        "created_at": p.created_at,
        "disk_count": len(p.disks),
        "vm_count": len(vms),
        "total_vcpus": total_vcpus,
        "total_ram_gb": total_ram_gb,
        "total_disk_gb": total_disk_gb,
        "is_ocp": is_ocp,
        "is_sno": is_sno,
        "recert": p.recert,
        "sync_status": sync_status,
        "source_provider_id": p.source_provider_id,
    }


def _pattern_to_detail_dict(p: Pattern, db: Session | None = None) -> dict:
    """Serialize a Pattern for detail responses (full)."""
    sync_status = None
    locations = []
    if db and p.source_provider_id:
        sync_status, locations = _compute_sync_status(p, db)

    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "owner_id": p.owner_id,
        "visibility": p.visibility,
        "source_project_id": p.source_project_id,
        "source_provider_id": p.source_provider_id,
        "topology": p.topology,
        "state": p.state,
        "capture_progress": (
            get_capture_progress(p.id) if p.state == "capturing" else None
        ),
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
        "sync_status": sync_status,
        "locations": locations,
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _resolve_pattern_source(body, user, db):
    """Return (source_project, topology, state) for pattern creation."""
    if body.source_project_id:
        query = db.query(Project).filter_by(id=body.source_project_id)
        if user.role != "admin":
            query = query.filter_by(owner_id=user.id)
        source_project = query.first()
        if not source_project:
            raise HTTPException(status_code=404, detail="Source project not found")
        if source_project.state not in ("active", "stopped"):
            raise HTTPException(
                status_code=400,
                detail="Project must be deployed (active or stopped) to save as pattern",
            )
        return source_project, source_project.topology or {}, "capturing"
    if body.topology:
        return None, body.topology, "available"
    raise HTTPException(status_code=400, detail="Provide source_project_id or topology")


@router.post(
    "/",
    status_code=201,
    responses={
        400: {
            "description": "Invalid request — project not deployed or missing source"
        },
        404: {"description": "Source project not found"},
        409: {"description": "Pattern with this name already exists"},
    },
)
def create_pattern(
    body: PatternCreate,
    user: CurrentUser,
    db: DbSession,
):
    """Create a pattern — either from a project (source_project_id) or from a
    raw topology+disk_mappings payload."""

    existing = db.query(Pattern).filter_by(owner_id=user.id, name=body.name).first()
    if existing:
        raise HTTPException(
            status_code=409, detail=f'You already have a pattern named "{body.name}"'
        )

    source_project, topology, state = _resolve_pattern_source(body, user, db)

    pattern_description = body.description or (
        source_project.description if source_project else None
    )

    clock_target = None
    if body.capture_clock_target and source_project and source_project.clock_target:
        clock_target = source_project.clock_target

    pattern = Pattern(
        name=body.name,
        description=pattern_description,
        owner_id=user.id,
        visibility=body.visibility,
        source_project_id=body.source_project_id,
        topology=topology,
        state=state,
        tags=body.tags,
        clock_target=clock_target,
        recert=body.recert,
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)

    # If capturing from project, kick off async disk capture
    if body.source_project_id:
        from app.core.redis import enqueue_job
        from app.services.pattern_service import capture_pattern_disks

        enqueue_job(
            capture_pattern_disks,
            pattern.id,
            body.source_project_id,
            body.restart_after,
            body.quiesce_cluster,
        )

    return _pattern_to_detail_dict(pattern, db)


@router.get("/")
def list_patterns(
    user: CurrentUser,
    db: DbSession,
    name: str | None = None,
    search: str | None = None,
    regex: str | None = None,
):
    """List patterns visible to the current user:
    - own patterns
    - patterns shared with them
    - public patterns
    Admin users see everything.

    Optional query parameters:
    - name: exact name match filter
    - search: prefix name search (case-insensitive)
    - regex: regex name match (PostgreSQL ~ operator)
    """
    if user.role == "admin":
        q = db.query(Pattern)
    else:
        shared_ids = [
            s.pattern_id
            for s in db.query(PatternShare.pattern_id).filter_by(user_id=user.id).all()
        ]
        q = db.query(Pattern).filter(
            or_(
                Pattern.owner_id == user.id,
                Pattern.id.in_(shared_ids) if shared_ids else sa_false(),
                Pattern.visibility == "public",
            )
        )

    if name is not None:
        q = q.filter(Pattern.name == name)
    elif search is not None:
        q = q.filter(Pattern.name.ilike(f"{search}%"))
    elif regex is not None:
        q = q.filter(Pattern.name.op("~")(regex))

    patterns = q.order_by(Pattern.created_at.desc()).all()

    return [_pattern_to_list_dict(p, db) for p in patterns]


@router.get(
    "/{pattern_id}",
    responses={404: {"description": "Pattern not found"}},
)
def get_pattern(
    pattern_id: str,
    user: CurrentUser,
    db: DbSession,
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    # Access check: owner, admin, shared, or public
    if (
        pattern.owner_id != user.id
        and user.role != "admin"
        and pattern.visibility != "public"
    ):
        shared = (
            db.query(PatternShare)
            .filter_by(pattern_id=pattern_id, user_id=user.id)
            .first()
        )
        if not shared:
            raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    return _pattern_to_detail_dict(pattern, db)


@router.get(
    "/{pattern_id}/export-template",
    responses={404: {"description": "Pattern not found"}},
)
def export_pattern_template(
    pattern_id: str,
    user: CurrentUser,
    db: DbSession,
):
    from app.services.template_loader import export_topology_to_template

    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    if (
        pattern.owner_id != user.id
        and user.role != "admin"
        and pattern.visibility != "public"
    ):
        shared = (
            db.query(PatternShare)
            .filter_by(pattern_id=pattern_id, user_id=user.id)
            .first()
        )
        if not shared:
            raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    topo = pattern.topology or {}
    result = export_topology_to_template(topo, db=db)
    result["name"] = pattern.name
    if pattern.description:
        result["description"] = pattern.description

    ocp_meta = topo.get("ocpMeta", {})
    if ocp_meta.get("clusterName"):
        result["ocp"] = {
            "cluster_name": ocp_meta["clusterName"],
            "base_domain": ocp_meta.get("baseDomain", "ocp.local"),
        }

    for key in ("disconnected", "bastion_services", "dns_records"):
        if topo.get(key):
            result[key] = topo[key]

    import yaml  # type: ignore[import-untyped]
    from fastapi.responses import Response

    yaml_str = yaml.dump(result, default_flow_style=False, sort_keys=False)
    header = "# Troshka infra_template export\n# WARNING: Passwords are stored in plain text.\n\n"
    return Response(content=header + yaml_str, media_type="text/yaml")


@router.get(
    "/{pattern_id}/export",
    responses={
        400: {"description": "Pattern not in available state"},
        404: {"description": "Pattern not found"},
    },
)
def export_pattern(
    pattern_id: str,
    user: CurrentUser,
    db: DbSession,
):
    """Export a pattern as a downloadable tar archive with topology + disk images."""
    from app.services.pattern_export import estimate_export_size, stream_pattern_export

    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    if (
        pattern.owner_id != user.id
        and user.role != "admin"
        and pattern.visibility != "public"
    ):
        shared = (
            db.query(PatternShare)
            .filter_by(pattern_id=pattern_id, user_id=user.id)
            .first()
        )
        if not shared:
            raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    if pattern.state != "available":
        raise HTTPException(
            status_code=400, detail="Pattern must be in 'available' state to export"
        )

    disks = (
        db.query(PatternDisk).filter_by(pattern_id=pattern_id, state="available").all()
    )

    filename = pattern.name.replace(" ", "_").replace("/", "_") + ".tar"
    content_length = estimate_export_size(pattern, disks)

    return StreamingResponse(
        stream_pattern_export(pattern_id, db),
        media_type="application/x-tar",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(content_length),
        },
    )


@router.post("/import", status_code=202)
def import_pattern(
    file: Annotated[UploadFile, File(...)],
    user: CurrentUser,
    db: DbSession,
    name: Annotated[str | None, Query()] = None,
):
    """Import a pattern from a tar archive upload."""
    from app.services.pattern_export import import_pattern_from_tar

    pattern = Pattern(
        name=name or "Importing...",
        owner_id=user.id,
        topology={"nodes": [], "edges": []},
        state="importing",
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)

    from app.core.redis import enqueue_job

    enqueue_job(import_pattern_from_tar, pattern.id, file.file, user.id, name)

    return {"id": pattern.id, "state": "importing", "name": pattern.name}


@router.patch(
    "/{pattern_id}",
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Pattern not found"},
    },
)
def update_pattern(
    pattern_id: str,
    body: PatternUpdate,
    user: CurrentUser,
    db: DbSession,
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)
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
    return _pattern_to_detail_dict(pattern, db)


@router.delete(
    "/{pattern_id}",
    status_code=204,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Pattern not found"},
    },
)
def delete_pattern(
    pattern_id: str,
    user: CurrentUser,
    db: DbSession,
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)
    if pattern.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    # Cancel in-flight capture jobs if pattern is still being captured
    if pattern.state in ("creating", "capturing"):
        from app.services.pattern_service import cancel_capture

        cancel_capture(pattern_id, db)

    # S3 cleanup — captured disks + any partially-uploaded files
    from app.services import s3_storage

    for disk in pattern.disks:
        try:
            s3_storage.delete_file(disk.s3_key)
        except Exception:
            logger.warning(
                "Failed to delete S3 object %s for pattern disk", disk.s3_key
            )
    try:
        s3_storage.delete_prefix(f"patterns/{pattern_id}/")
    except Exception:
        logger.warning(
            "Failed to clean S3 prefix patterns/%s/", sanitize_log(pattern_id[:8])
        )

    # Cluster RGW cleanup — read locations before cascade delete destroys them
    from app.services import cluster_storage

    cluster_provider_ids = {
        loc.provider_id
        for disk in pattern.disks
        for loc in disk.locations
        if loc.provider_id is not None
    }
    for pid in cluster_provider_ids:
        cluster_storage.delete_pattern(db, pid, pattern_id)

    db.delete(pattern)
    db.commit()

    # Clean pattern cache on all hosts in background
    from app.core.redis import enqueue_job
    from app.workers.jobs import job_clean_pattern_cache

    enqueue_job(job_clean_pattern_cache, pattern_id, queue_name="default")


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


@router.post(
    "/{pattern_id}/share",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Only the owner can share"},
        404: {"description": "Pattern or user not found"},
    },
)
def share_pattern(
    pattern_id: str,
    body: PatternShareRequest,
    user: CurrentUser,
    db: DbSession,
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)
    if pattern.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can share")

    target_user = db.query(User).filter_by(email=body.user_email).first()
    if not target_user:
        raise HTTPException(status_code=404, detail=f"User {body.user_email} not found")
    if target_user.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot share with yourself")

    existing = (
        db.query(PatternShare)
        .filter_by(pattern_id=pattern_id, user_id=target_user.id)
        .first()
    )
    if not existing:
        db.add(PatternShare(pattern_id=pattern_id, user_id=target_user.id))
        db.commit()

    return {"shared_with": body.user_email}


@router.delete(
    "/{pattern_id}/share/{user_email}",
    responses={
        403: {"description": "Only the owner can revoke sharing"},
        404: {"description": "Pattern or user not found"},
    },
)
def revoke_share(
    pattern_id: str,
    user_email: str,
    user: CurrentUser,
    db: DbSession,
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)
    if pattern.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can revoke sharing")

    target_user = db.query(User).filter_by(email=user_email).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    share = (
        db.query(PatternShare)
        .filter_by(pattern_id=pattern_id, user_id=target_user.id)
        .first()
    )
    if share:
        db.delete(share)
        db.commit()

    return {"unshared": user_email}


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


@router.get(
    "/{pattern_id}/progress",
    responses={404: {"description": "Pattern not found"}},
)
def pattern_progress(
    pattern_id: str,
    user: CurrentUser,
    db: DbSession,
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)
    if (
        pattern.owner_id != user.id
        and user.role != "admin"
        and pattern.visibility != "public"
    ):
        shared = (
            db.query(PatternShare)
            .filter_by(pattern_id=pattern_id, user_id=user.id)
            .first()
        )
        if not shared:
            raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    progress = get_capture_progress(pattern_id)
    if progress is None:
        return {"pattern_id": pattern_id, "state": pattern.state, "progress": None}

    return {"pattern_id": pattern_id, "state": pattern.state, "progress": progress}


# ---------------------------------------------------------------------------
# Deploy — create a single project from a pattern
# ---------------------------------------------------------------------------


def _find_bastion_vm(nodes: list) -> dict | None:
    """Find the first bastion VM node."""
    for n in nodes:
        if n.get("type") != "vmNode":
            continue
        tags = n.get("data", {}).get("tags", {})
        groups = tags.get("AnsibleGroup", "")
        if "bastions" in [g.strip() for g in groups.split(",")]:
            return n
    return None


def _find_cloud_init_vm(nodes: list) -> dict | None:
    """Find the first cloud-init VM node."""
    for n in nodes:
        if n.get("type") == "vmNode" and n.get("data", {}).get("cloudInit"):
            return n
    return None


def _apply_inject_vars(nodes: list, inject_vars: dict) -> None:
    """Find the best target VM and set ciInjectVars on it.

    Priority: bastion VM first, then any cloud-init VM.
    """
    target_vm = _find_bastion_vm(nodes)
    if target_vm is None:
        target_vm = _find_cloud_init_vm(nodes)
    if target_vm is not None:
        target_vm["data"]["ciInjectVars"] = inject_vars


def _check_pattern_access(
    pattern: Pattern, user: User, pattern_id: str, db: Session
) -> None:
    """Raise HTTPException if user does not have access to pattern."""
    if (
        pattern.owner_id == user.id
        or user.role == "admin"
        or pattern.visibility == "public"
    ):
        return
    shared = (
        db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=user.id).first()
    )
    if not shared:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)


def _apply_common_password(nodes: list, common_password: str) -> None:
    """Apply common password to BMC networks and cloud-init VMs."""
    for n in nodes:
        d = n.get("data", {})
        if n.get("type") == "networkNode" and d.get("networkType") == "bmc":
            d["bmcPassword"] = common_password
        elif n.get("type") == "vmNode" and d.get("cloudInit"):
            d["ciCloudUserPassword"] = common_password


def _apply_ssh_keys(nodes: list, ssh_keys: list) -> None:
    """Merge SSH keys into cloud-init VMs."""
    for n in nodes:
        if n.get("type") != "vmNode":
            continue
        if not n.get("data", {}).get("cloudInit"):
            continue
        existing = n["data"].get("ciSshKeys", [])
        n["data"]["ciSshKeys"] = list(set(existing + ssh_keys))


def _apply_optional_fields(
    project: Project, pattern: Pattern, body: PatternDeployRequest
) -> None:
    """Apply optional fields to project from pattern and request body."""
    if pattern.clock_target:
        project.clock_target = pattern.clock_target
    if body.guid:
        project.guid = body.guid
    if body.domain:
        project.domain = body.domain
    if body.dns_provider_id:
        project.dns_provider_id = body.dns_provider_id


def _apply_recert_config(project: Project, body: PatternDeployRequest) -> None:
    """Apply recert and common_password to topology if specified."""
    if body.recert is None:
        return
    topo = project.topology or {}
    topo["_deploy_recert"] = body.recert
    if body.common_password:
        topo["_deploy_common_password"] = body.common_password
    project.topology = topo


def _start_auto_deploy(
    project: Project, body: PatternDeployRequest, db: Session
) -> None:
    """Start async deployment if auto_deploy is requested."""
    from app.core.redis import enqueue_job
    from app.services.deploy_service import deploy_project_async

    if body.host_id:
        project.host_id = body.host_id
    project.state = "deploying"
    project.deploy_started_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    enqueue_job(
        deploy_project_async, project.id, body.auto_start, project_id=project.id
    )


@router.post(
    "/{pattern_id}/deploy",
    status_code=201,
    responses={
        404: {"description": "Pattern not found"},
        409: {"description": "Project with this name already exists"},
    },
)
def deploy_pattern(
    pattern_id: str,
    body: PatternDeployRequest,
    user: CurrentUser,
    db: DbSession,
):
    """Create a new project in 'draft' state from a pattern.

    Clones the topology with all-new UUIDs, regenerated MACs, and fresh
    disk-controller IDs while preserving network configuration.
    """
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    _check_pattern_access(pattern, user, pattern_id, db)

    project_name = body.name or f"{pattern.name} (deploy)"
    existing = db.query(Project).filter_by(owner_id=user.id, name=project_name).first()
    if existing:
        raise HTTPException(
            status_code=409, detail=f'You already have a project named "{project_name}"'
        )

    new_topology = _remap_topology(pattern.topology)
    nodes = new_topology.get("nodes", [])

    if body.common_password:
        _apply_common_password(nodes, body.common_password)
    if body.ssh_keys:
        _apply_ssh_keys(nodes, body.ssh_keys)
    if body.inject_vars:
        _apply_inject_vars(nodes, body.inject_vars)

    project = Project(
        name=project_name,
        description=body.description or pattern.description,
        owner_id=user.id,
        topology=new_topology,
        state="draft",
    )

    _apply_optional_fields(project, pattern, body)

    db.add(project)
    db.commit()
    db.refresh(project)

    _apply_recert_config(project, body)

    if body.auto_deploy:
        _start_auto_deploy(project, body, db)

    return {
        "id": project.id,
        "name": project.name,
        "state": project.state,
        "topology": project.topology,
    }


# ---------------------------------------------------------------------------
# Bulk Deploy — create N projects from a pattern
# ---------------------------------------------------------------------------


def _bulk_deploy_projects(project_ids: list[str]):
    from app.core.database import SessionLocal
    from app.services.deploy_service import deploy_project_async
    from app.services.placement import calculate_project_requirements, place_project

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
                logger.warning(
                    "Bulk deploy: placement failed for %s: %s",
                    project_id[:8],
                    result["error"],
                )
                project.state = "error"
                project.deploy_error = result["error"]
                continue
            project.vni_map = result.get("vni_map")
            project.state = "deploying"
            project.deploy_started_at = datetime.datetime.now(datetime.UTC)
            ready_ids.append(project_id)
        s.commit()
    except Exception:
        logger.exception("Bulk deploy: placement phase failed")
        return
    finally:
        s.close()

    from app.core.redis import enqueue_job

    for pid in ready_ids:
        enqueue_job(deploy_project_async, pid, project_id=pid)


def _create_bulk_project(
    pattern: Pattern, body: PatternBulkDeployRequest, user: User, index: int
) -> Project:
    """Create a single project from pattern for bulk deployment."""
    name = body.name_template.replace("{n}", f"{index:03d}")
    new_topology = _remap_topology(pattern.topology)
    project = Project(
        name=name,
        description=pattern.description,
        owner_id=user.id,
        topology=new_topology,
        state="draft",
    )
    if body.guid_template:
        project.guid = body.guid_template.replace("{n}", f"{index:03d}")
    if body.domain:
        project.domain = body.domain
    if body.dns_provider_id:
        project.dns_provider_id = body.dns_provider_id
    return project


@router.post(
    "/{pattern_id}/bulk-deploy",
    status_code=201,
    responses={
        400: {"description": "Invalid count"},
        404: {"description": "Pattern not found"},
    },
)
def bulk_deploy_pattern(
    pattern_id: str,
    body: PatternBulkDeployRequest,
    user: CurrentUser,
    db: DbSession,
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
        raise HTTPException(status_code=404, detail=_PATTERN_NOT_FOUND)

    _check_pattern_access(pattern, user, pattern_id, db)

    projects = []
    for i in range(1, body.count + 1):
        project = _create_bulk_project(pattern, body, user, i)
        db.add(project)
        projects.append(project)

    db.commit()
    for p in projects:
        db.refresh(p)

    if body.auto_deploy:
        project_ids = [p.id for p in projects]
        from app.core.redis import enqueue_job
        from app.workers.jobs import job_bulk_deploy_projects

        enqueue_job(job_bulk_deploy_projects, project_ids)

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
