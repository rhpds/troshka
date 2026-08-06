from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.library import Library, LibraryItem
from app.models.project import Project
from app.models.user import User
from app.models.vm import VM
from app.schemas.library import SnapshotCreate, SnapshotResponse
from app.schemas.vm import VMCreate, VMResponse, VMUpdate

router = APIRouter(prefix="/projects/{project_id}/vms", tags=["vms"])

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]

_VM_NOT_FOUND = "VM not found"


def _get_project_or_403(project_id: str, user: User, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.get(
    "/",
    response_model=list[VMResponse],
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project not found"},
    },
)
def list_vms(project_id: str, user: CurrentUser, db: DbSession):
    _get_project_or_403(project_id, user, db)
    return db.query(VM).filter_by(project_id=project_id).all()


@router.post(
    "/",
    response_model=VMResponse,
    status_code=201,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project not found"},
    },
)
def create_vm(project_id: str, body: VMCreate, user: CurrentUser, db: DbSession):
    _get_project_or_403(project_id, user, db)
    vm = VM(project_id=project_id, **body.model_dump())
    db.add(vm)
    db.commit()
    db.refresh(vm)
    return vm


@router.get(
    "/{vm_id}",
    response_model=VMResponse,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project or VM not found"},
    },
)
def get_vm(project_id: str, vm_id: str, user: CurrentUser, db: DbSession):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail=_VM_NOT_FOUND)
    return vm


@router.patch(
    "/{vm_id}",
    response_model=VMResponse,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project or VM not found"},
    },
)
def update_vm(
    project_id: str, vm_id: str, body: VMUpdate, user: CurrentUser, db: DbSession
):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail=_VM_NOT_FOUND)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(vm, field, value)
    db.commit()
    db.refresh(vm)
    return vm


@router.delete(
    "/{vm_id}",
    status_code=204,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project or VM not found"},
    },
)
def delete_vm(project_id: str, vm_id: str, user: CurrentUser, db: DbSession):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail=_VM_NOT_FOUND)
    db.delete(vm)
    db.commit()


def _is_edge_between(edge: dict, node_a: str, node_b: str) -> bool:
    """Check if an edge connects two nodes in either direction."""
    return (edge.get("source") == node_a and edge.get("target") == node_b) or (
        edge.get("target") == node_a and edge.get("source") == node_b
    )


def _find_vm_node(topology: dict, vm_id: str) -> dict | None:
    """Find a VM node by ID in topology."""
    for node in topology.get("nodes", []):
        if node["id"] == vm_id and node.get("type") == "vmNode":
            return node
    return None


def _find_connected_disks(topology: dict, vm_id: str) -> list[dict]:
    """Collect disk info for all storage nodes connected to a VM."""
    edges = topology.get("edges", [])
    disks: list[dict] = []
    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        if not any(_is_edge_between(e, vm_id, node["id"]) for e in edges):
            continue
        d = node.get("data", {})
        disks.append(
            {
                "name": d.get("name", "disk"),
                "size": d.get("size", 20),
                "format": d.get("format", "qcow2"),
                "source": d.get("source"),
                "libraryItemId": d.get("libraryItemId"),
                "libraryItemName": d.get("libraryItemName"),
            }
        )
    return disks


def _find_connecting_edge(edges: list[dict], node_a: str, node_b: str) -> dict | None:
    """Find the first edge connecting two nodes in either direction."""
    for e in edges:
        if _is_edge_between(e, node_a, node_b):
            return e
    return None


def _find_connected_networks(topology: dict, vm_id: str) -> list[dict]:
    """Collect network info for all network nodes connected to a VM."""
    edges = topology.get("edges", [])
    networks: list[dict] = []
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        edge = _find_connecting_edge(edges, vm_id, node["id"])
        if not edge:
            continue
        d = node.get("data", {})
        nic_handle = (
            edge.get("sourceHandle")
            if edge.get("source") == vm_id
            else edge.get("targetHandle")
        )
        networks.append(
            {
                "name": d.get("name", "network"),
                "cidr": d.get("cidr", ""),
                "nicHandle": nic_handle,
            }
        )
    return networks


def _ensure_user_library(user: User, db: Session) -> Library:
    """Get or create the user's personal library."""
    lib = db.query(Library).filter_by(owner_id=user.id, type="personal").first()
    if lib:
        return lib
    lib = Library(type="personal", owner_id=user.id)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


@router.post(
    "/{vm_id}/snapshot",
    response_model=SnapshotResponse,
    status_code=201,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project or VM not found"},
        409: {"description": "Duplicate snapshot name"},
    },
)
def snapshot_vm(
    project_id: str, vm_id: str, body: SnapshotCreate, user: CurrentUser, db: DbSession
):
    project = _get_project_or_403(project_id, user, db)

    topology = project.topology or {"nodes": [], "edges": []}
    vm_node = _find_vm_node(topology, vm_id)
    if not vm_node:
        raise HTTPException(status_code=404, detail="VM not found in topology")

    vm_data = vm_node.get("data", {})
    vm_config = {
        "vcpus": vm_data.get("vcpus"),
        "ram": vm_data.get("ram"),
        "os": vm_data.get("os"),
        "nics": vm_data.get("nics", []),
        "diskControllers": vm_data.get("diskControllers", []),
        "bootMethod": vm_data.get("bootMethod"),
        "cloudInit": vm_data.get("cloudInit"),
        "consoleType": vm_data.get("consoleType"),
        "autoStart": vm_data.get("autoStart"),
        "disks": _find_connected_disks(topology, vm_id),
        "networks": _find_connected_networks(topology, vm_id),
    }

    lib = _ensure_user_library(user, db)

    existing = (
        db.query(LibraryItem).filter_by(library_id=lib.id, name=body.name).first()
    )
    if existing:
        raise HTTPException(
            status_code=409, detail=f'You already have a snapshot named "{body.name}"'
        )

    item = LibraryItem(
        library_id=lib.id,
        name=body.name,
        description=body.description,
        type="snapshot",
        format="qcow2",
        state="uploading",
        source_vm_id=vm_id,
        vm_config=vm_config,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    if project.state in ("active", "stopped"):
        from app.core.redis import enqueue_job
        from app.services.snapshot_service import capture_vm_disks

        enqueue_job(capture_vm_disks, item.id, project.id, vm_id)
    else:
        item.state = "available"
        db.commit()

    return item
