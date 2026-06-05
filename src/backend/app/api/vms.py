from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.models.vm import VM
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
