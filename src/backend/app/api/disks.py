from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.disk import Disk
from app.models.project import Project
from app.models.user import User
from app.schemas.disk import DiskCreate, DiskResponse, DiskUpdate

router = APIRouter(prefix="/projects/{project_id}/disks", tags=["disks"])


def _get_project_or_403(project_id: str, user: User, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.get("/", response_model=list[DiskResponse])
def list_disks(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    return db.query(Disk).filter_by(project_id=project_id).all()


@router.post("/", response_model=DiskResponse, status_code=201)
def create_disk(
    project_id: str,
    body: DiskCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = Disk(project_id=project_id, **body.model_dump())
    db.add(disk)
    db.commit()
    db.refresh(disk)
    return disk


@router.get("/{disk_id}", response_model=DiskResponse)
def get_disk(
    project_id: str,
    disk_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    return disk


@router.patch("/{disk_id}", response_model=DiskResponse)
def update_disk(
    project_id: str,
    disk_id: str,
    body: DiskUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(disk, field, value)
    db.commit()
    db.refresh(disk)
    return disk


@router.delete("/{disk_id}", status_code=204)
def delete_disk(
    project_id: str,
    disk_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    db.delete(disk)
    db.commit()


@router.post("/{disk_id}/attach/{vm_id}", response_model=DiskResponse)
def attach_disk(
    project_id: str,
    disk_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    disk.vm_id = vm_id
    disk.attached = True
    db.commit()
    db.refresh(disk)
    return disk


@router.post("/{disk_id}/detach", response_model=DiskResponse)
def detach_disk(
    project_id: str,
    disk_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    disk.vm_id = None
    disk.attached = False
    db.commit()
    db.refresh(disk)
    return disk
