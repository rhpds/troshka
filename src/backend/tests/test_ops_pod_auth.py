"""Tests for the ops-pod key mint/revoke service (Plan 4, Task 3).

`app.services.ocp.ops_pod_auth` mints a project-scoped, least-privilege
API key for the in-cluster ops pod and revokes it. Minting is idempotent:
it never leaves two active ops-pod keys for the same project.
"""

import uuid

from app.models.api_key import ApiKey, hash_key
from app.models.project import Project
from app.models.user import User
from app.services.ocp.ops_pod_auth import (
    OPS_POD_SCOPES,
    _ops_pod_key_name,
    mint_ops_pod_key,
    revoke_ops_pod_key,
)
from tests.conftest import TestSession


def _make_owner(db):
    user = User(
        id=str(uuid.uuid4()),
        email=f"owner-{uuid.uuid4().hex[:8]}@troshka",
        display_name="owner",
        role="user",
        auth_source="sso",
    )
    db.add(user)
    db.commit()
    return user


def _make_project(db, owner_id):
    p = Project(
        id=str(uuid.uuid4()),
        name=f"proj-{uuid.uuid4().hex[:8]}",
        state="active",
        owner_id=owner_id,
    )
    db.add(p)
    db.commit()
    return p


def _active_ops_keys(db, project_id):
    return (
        db.query(ApiKey)
        .filter_by(name=_ops_pod_key_name(project_id), is_active=True)
        .all()
    )


def test_mint_returns_trk_key_and_stores_scoped_row():
    db = TestSession()
    try:
        owner = _make_owner(db)
        project = _make_project(db, owner.id)

        raw = mint_ops_pod_key(db, project)

        assert raw.startswith("trk_")

        rows = _active_ops_keys(db, project.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.project_id == project.id
        assert row.scopes == OPS_POD_SCOPES
        assert row.is_active is True
        assert row.name == _ops_pod_key_name(project.id)
        assert row.user_id == owner.id
        # The raw key hashes to the stored hash, and prefix matches.
        assert row.key_hash == hash_key(raw)
        assert row.key_prefix == raw[:10]
    finally:
        db.close()


def test_mint_is_idempotent_single_active_key():
    db = TestSession()
    try:
        owner = _make_owner(db)
        project = _make_project(db, owner.id)

        raw1 = mint_ops_pod_key(db, project)
        raw2 = mint_ops_pod_key(db, project)

        assert raw1 != raw2

        active = _active_ops_keys(db, project.id)
        assert len(active) == 1
        # The surviving active key is the second (freshly minted) one.
        assert active[0].key_hash == hash_key(raw2)

        # The first key was revoked, not deleted.
        old = db.query(ApiKey).filter_by(key_hash=hash_key(raw1)).one()
        assert old.is_active is False
    finally:
        db.close()


def test_revoke_deactivates_and_returns_count():
    db = TestSession()
    try:
        owner = _make_owner(db)
        project = _make_project(db, owner.id)
        mint_ops_pod_key(db, project)

        count = revoke_ops_pod_key(db, project.id)

        assert count == 1
        assert _active_ops_keys(db, project.id) == []
    finally:
        db.close()


def test_revoke_when_none_returns_zero():
    db = TestSession()
    try:
        owner = _make_owner(db)
        project = _make_project(db, owner.id)

        assert revoke_ops_pod_key(db, project.id) == 0
    finally:
        db.close()
