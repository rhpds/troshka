"""Mint and revoke the project-scoped ops-pod API key (Plan 4, Task 3).

The in-cluster ops pod authenticates to Troshka with a least-privilege,
project-scoped API key that can only read its own project's topology and
exec into that project's VMs (see `ApiKey.has_scope` / the auth-layer
default-deny enforcement). This module owns the lifecycle of that key.

Minting is idempotent: any existing active ops-pod key for the project is
revoked first, so there is never more than one active ops-pod key per
project. The raw `trk_...` secret is returned exactly once (only its hash
is persisted); the caller must inject it into the pod immediately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.api_key import ApiKey, generate_api_key, hash_key

if TYPE_CHECKING:
    from app.models.project import Project

# Least-privilege scopes granted to the ops-pod key. These must match the
# permissions the auth-layer allowlist checks (topology read + VM exec).
OPS_POD_SCOPES: list[str] = ["topology:read", "vm:exec"]


def _ops_pod_key_name(project_id: str) -> str:
    """Deterministic, per-project name used to find/rotate the ops-pod key."""
    return f"ops-pod:{project_id}"


def mint_ops_pod_key(db: Session, project: Project) -> str:
    """Create (rotating any existing) ops-pod key for `project`.

    Revokes any currently active ops-pod key(s) for the project first, then
    creates a fresh scoped key owned by the project owner. Returns the raw
    `trk_...` secret once; only its hash is stored.
    """
    revoke_ops_pod_key(db, project.id)

    raw = generate_api_key()
    api_key = ApiKey(
        user_id=project.owner_id,
        name=_ops_pod_key_name(project.id),
        key_hash=hash_key(raw),
        key_prefix=raw[:10],
        is_active=True,
        project_id=project.id,
        scopes=OPS_POD_SCOPES,
    )
    db.add(api_key)
    db.commit()
    return raw


def revoke_ops_pod_key(db: Session, project_id: str) -> int:
    """Deactivate all active ops-pod key(s) for the project.

    Returns the number of keys deactivated. Idempotent: returns 0 when there
    is nothing to revoke.
    """
    keys = (
        db.query(ApiKey)
        .filter_by(name=_ops_pod_key_name(project_id), is_active=True)
        .all()
    )
    for key in keys:
        key.is_active = False
    if keys:
        db.commit()
    return len(keys)
