from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas.user import UserCreate, UserResponse, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])

VALID_ROLES = {"user", "operator", "admin"}


@router.get("/", response_model=list[UserResponse])
def list_users(
    user: User = Depends(require_role("admin")), db: Session = Depends(get_db)
):
    return db.query(User).order_by(User.email).all()


@router.post("/", response_model=UserResponse, status_code=201)
def create_user(
    body: UserCreate,
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(400, "Email is required")
    if body.role not in VALID_ROLES:
        raise HTTPException(
            400,
            f"Invalid role: {body.role}. Must be one of: {', '.join(sorted(VALID_ROLES))}",
        )
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(409, "A user with this email already exists")
    user = User(
        email=email,
        display_name=body.display_name or email.split("@")[0],
        role=body.role,
        auth_source="invited",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    body: UserUpdate,
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(
                400,
                f"Invalid role: {body.role}. Must be one of: {', '.join(sorted(VALID_ROLES))}",
            )
        if user_id == current_user.id:
            raise HTTPException(400, "Cannot change your own role")
        user.role = body.role
    if body.display_name is not None:
        user.display_name = body.display_name
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: str,
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user_id == current_user.id:
        raise HTTPException(400, "Cannot delete yourself")
    project_count = db.query(Project).filter_by(owner_id=user_id).count()
    if project_count > 0:
        raise HTTPException(
            409,
            f"User has {project_count} project(s) — reassign or delete them first",
        )
    db.delete(user)
    db.commit()
