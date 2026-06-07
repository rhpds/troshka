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


def _get_project_or_403(project_id: str, user: User, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.get("/", response_model=list[VMResponse])
def list_vms(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    return db.query(VM).filter_by(project_id=project_id).all()


@router.post("/", response_model=VMResponse, status_code=201)
def create_vm(project_id: str, body: VMCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    vm = VM(project_id=project_id, **body.model_dump())
    db.add(vm)
    db.commit()
    db.refresh(vm)
    return vm


@router.get("/{vm_id}", response_model=VMResponse)
def get_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm


@router.patch("/{vm_id}", response_model=VMResponse)
def update_vm(project_id: str, vm_id: str, body: VMUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(vm, field, value)
    db.commit()
    db.refresh(vm)
    return vm


@router.delete("/{vm_id}", status_code=204)
def delete_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    db.delete(vm)
    db.commit()


@router.post("/{vm_id}/snapshot", response_model=SnapshotResponse, status_code=201)
def snapshot_vm(
    project_id: str,
    vm_id: str,
    body: SnapshotCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    topology = project.topology or {"nodes": [], "edges": []}
    vm_node = None
    for node in topology.get("nodes", []):
        if node["id"] == vm_id and node.get("type") == "vmNode":
            vm_node = node
            break
    if not vm_node:
        raise HTTPException(status_code=404, detail="VM not found in topology")

    vm_data = vm_node.get("data", {})

    edges = topology.get("edges", [])
    connected_disks = []
    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        connected = any(
            (e.get("source") == vm_id and e.get("target") == node["id"])
            or (e.get("target") == vm_id and e.get("source") == node["id"])
            for e in edges
        )
        if connected:
            d = node.get("data", {})
            connected_disks.append({
                "name": d.get("name", "disk"),
                "size": d.get("size", 20),
                "format": d.get("format", "qcow2"),
                "source": d.get("source"),
                "libraryItemId": d.get("libraryItemId"),
                "libraryItemName": d.get("libraryItemName"),
            })

    connected_networks = []
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        connected_edge = None
        for e in edges:
            if (e.get("source") == vm_id and e.get("target") == node["id"]) or \
               (e.get("target") == vm_id and e.get("source") == node["id"]):
                connected_edge = e
                break
        if connected_edge:
            d = node.get("data", {})
            nic_handle = connected_edge.get("sourceHandle") if connected_edge.get("source") == vm_id else connected_edge.get("targetHandle")
            connected_networks.append({
                "name": d.get("name", "network"),
                "cidr": d.get("cidr", ""),
                "nicHandle": nic_handle,
            })

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
        "disks": connected_disks,
        "networks": connected_networks,
    }

    lib = db.query(Library).filter_by(owner_id=user.id, type="personal").first()
    if not lib:
        lib = Library(type="personal", owner_id=user.id)
        db.add(lib)
        db.commit()
        db.refresh(lib)

    existing = db.query(LibraryItem).filter_by(library_id=lib.id, name=body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"You already have a snapshot named \"{body.name}\"")

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
        import threading
        from app.services.snapshot_service import capture_vm_disks

        threading.Thread(
            target=capture_vm_disks,
            args=(item.id, project.id, vm_id),
            daemon=True,
        ).start()
    else:
        item.state = "available"
        db.commit()

    return item
