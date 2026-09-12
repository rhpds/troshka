"""Mint a per-project scoped API key for the workload runner pod."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.api_key import ApiKey, generate_api_key, hash_key

if TYPE_CHECKING:
    from app.models.project import Project

RUN_KEY_SCOPES = ["topology:read", "vm:exec", "cluster:access"]


def _key_name(project_id: str) -> str:
    return f"workload-run:{project_id}"


def revoke_run_key(db: Session, project: Project) -> None:
    rows = db.query(ApiKey).filter_by(name=_key_name(project.id)).all()
    for row in rows:
        row.is_active = False
    if rows:
        db.commit()


def mint_run_key(db: Session, project: Project) -> str:
    revoke_run_key(db, project)
    raw = generate_api_key()
    key = ApiKey(
        user_id=project.owner_id,
        name=_key_name(project.id),
        key_hash=hash_key(raw),
        key_prefix=raw[:10],
        project_id=project.id,
        scopes=RUN_KEY_SCOPES,
        is_active=True,
    )
    db.add(key)
    db.commit()
    return raw
