from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.network import Network
from app.models.project import Project
from app.models.user import User
from app.schemas.network import NetworkCreate, NetworkResponse, NetworkUpdate

router = APIRouter(prefix="/projects/{project_id}/networks", tags=["networks"])

_NETWORK_NOT_FOUND = "Network not found"

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]


def _get_project_or_403(project_id: str, user: User, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.get(
    "/",
    response_model=list[NetworkResponse],
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project not found"},
    },
)
def list_networks(project_id: str, user: CurrentUser, db: DbSession):
    _get_project_or_403(project_id, user, db)
    return db.query(Network).filter_by(project_id=project_id).all()


@router.post(
    "/",
    response_model=NetworkResponse,
    status_code=201,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project not found"},
    },
)
def create_network(
    project_id: str, body: NetworkCreate, user: CurrentUser, db: DbSession
):
    _get_project_or_403(project_id, user, db)
    network = Network(project_id=project_id, **body.model_dump())
    db.add(network)
    db.commit()
    db.refresh(network)
    return network


@router.get(
    "/{network_id}",
    response_model=NetworkResponse,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project or network not found"},
    },
)
def get_network(project_id: str, network_id: str, user: CurrentUser, db: DbSession):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail=_NETWORK_NOT_FOUND)
    return network


@router.patch(
    "/{network_id}",
    response_model=NetworkResponse,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project or network not found"},
    },
)
def update_network(
    project_id: str,
    network_id: str,
    body: NetworkUpdate,
    user: CurrentUser,
    db: DbSession,
):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail=_NETWORK_NOT_FOUND)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(network, field, value)
    db.commit()
    db.refresh(network)
    return network


@router.delete(
    "/{network_id}",
    status_code=204,
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Project or network not found"},
    },
)
def delete_network(project_id: str, network_id: str, user: CurrentUser, db: DbSession):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail=_NETWORK_NOT_FOUND)
    db.delete(network)
    db.commit()
