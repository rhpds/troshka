import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.services.template_loader import (
    generate_topology_from_template,
    resolve_template,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["templates"])


class DeployTemplateRequest(BaseModel):
    template: str
    version: str
    name: str
    description: str | None = None
    overrides: dict | None = None
    auto_deploy: bool = False
    auto_start: bool = True


@router.post("/deploy-template", status_code=201)
def deploy_template(
    body: DeployTemplateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        resolved = resolve_template(
            body.template, overrides=body.overrides, version=body.version
        )
    except FileNotFoundError:
        raise HTTPException(400, f"Unknown template: {body.template}")
    except ValueError as e:
        raise HTTPException(400, str(e))

    topology = generate_topology_from_template(resolved)

    existing = db.query(Project).filter_by(owner_id=user.id, name=body.name).first()
    if existing:
        raise HTTPException(409, f"Project named '{body.name}' already exists")

    project = Project(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        topology=topology,
        state="draft",
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    return {
        "id": project.id,
        "name": project.name,
        "state": project.state,
        "topology": project.topology,
    }
