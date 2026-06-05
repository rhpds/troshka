from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.network import Network
from app.models.project import Project
from app.models.user import User
from app.schemas.network import NetworkCreate, NetworkResponse, NetworkUpdate

router = APIRouter(prefix="/projects/{project_id}/networks", tags=["networks"])


def _get_project_or_403(project_id: str, user: User, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.get("/", response_model=list[NetworkResponse])
def list_networks(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    return db.query(Network).filter_by(project_id=project_id).all()


@router.post("/", response_model=NetworkResponse, status_code=201)
def create_network(project_id: str, body: NetworkCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    network = Network(project_id=project_id, **body.model_dump())
    db.add(network)
    db.commit()
    db.refresh(network)
    return network


@router.get("/{network_id}", response_model=NetworkResponse)
def get_network(project_id: str, network_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    return network


@router.patch("/{network_id}", response_model=NetworkResponse)
def update_network(project_id: str, network_id: str, body: NetworkUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(network, field, value)
    db.commit()
    db.refresh(network)
    return network


@router.delete("/{network_id}", status_code=204)
def delete_network(project_id: str, network_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    db.delete(network)
    db.commit()
