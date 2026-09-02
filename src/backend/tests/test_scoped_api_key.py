"""Tests for project-scoped, limited-permission API keys.

Covers the ApiKey.is_scoped property and ApiKey.has_scope() helper that back
the per-project least-privilege ops-pod key (Plan 4, Task 1), plus the
enforcement of that scope in the auth layer (Plan 4, Task 2).
"""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.auth import scoped_key_router_guard
from app.core.database import get_db
from app.main import app
from app.models.api_key import ApiKey, generate_api_key, hash_key
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _make_key(project_id=None, scopes=None):
    key = generate_api_key()
    return ApiKey(
        id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        name="test-key",
        key_hash=hash_key(key),
        key_prefix=key[:10],
        project_id=project_id,
        scopes=scopes,
    )


def test_unscoped_key_is_full_access():
    """A key with no project_id behaves like today's full-access user key."""
    key = _make_key(project_id=None, scopes=None)
    assert key.is_scoped is False
    # Full access: has_scope returns True for anything.
    assert key.has_scope("topology:read") is True
    assert key.has_scope("vm:exec") is True
    assert key.has_scope("anything:at:all") is True


def test_scoped_key_grants_only_listed_permissions():
    """A project-scoped key only grants the permissions in its scopes list."""
    key = _make_key(project_id="p1", scopes=["topology:read"])
    assert key.is_scoped is True
    assert key.has_scope("topology:read") is True
    assert key.has_scope("vm:exec") is False


def test_scoped_key_with_empty_scopes_grants_nothing():
    """A scoped key with an empty scopes list grants no permissions."""
    key = _make_key(project_id="p1", scopes=[])
    assert key.is_scoped is True
    assert key.has_scope("topology:read") is False


def test_scoped_key_with_none_scopes_grants_nothing():
    """A scoped key (project set) with scopes=None grants no permissions."""
    key = _make_key(project_id="p1", scopes=None)
    assert key.is_scoped is True
    assert key.has_scope("topology:read") is False


# ---------------------------------------------------------------------------
# scoped_key_router_guard — unit tests against a fake Request
# ---------------------------------------------------------------------------
class _FakeState:
    def __init__(self, api_key):
        self.api_key = api_key


class _FakeRoute:
    def __init__(self, name):
        self.name = name


class _FakeRequest:
    def __init__(self, api_key, project_id, route_name):
        self.state = _FakeState(api_key)
        self.path_params = {"project_id": project_id}
        self.scope = {"route": _FakeRoute(route_name)}


def _run_guard(api_key, route_project_id, route_name):
    req = _FakeRequest(api_key, route_project_id, route_name)
    # _user arg is unused by the guard body; pass a sentinel.
    return scoped_key_router_guard(req, _user=object())


def test_guard_noop_when_no_api_key():
    """JWT/dev auth (no api_key on request.state) is a pure no-op."""
    assert _run_guard(None, "pA", "get_project") is None


def test_guard_noop_for_unscoped_key():
    """Unscoped (full-access) keys bypass scope enforcement entirely."""
    key = _make_key(project_id=None, scopes=None)
    assert _run_guard(key, "pA", "delete_project") is None


def test_guard_allows_allowlisted_route_with_matching_project_and_perm():
    key = _make_key(project_id="pA", scopes=["topology:read", "vm:exec"])
    assert _run_guard(key, "pA", "get_project") is None
    assert _run_guard(key, "pA", "vm_exec") is None


def test_guard_default_denies_unlisted_route():
    """A route not in the allowlist is 403 even with matching project."""
    key = _make_key(project_id="pA", scopes=["topology:read", "vm:exec"])
    try:
        _run_guard(key, "pA", "delete_project")
        raise AssertionError("expected HTTPException for un-allowlisted route")
    except HTTPException as exc:
        assert exc.status_code == 403


def test_guard_blocks_wrong_project():
    key = _make_key(project_id="pA", scopes=["topology:read"])
    try:
        _run_guard(key, "pB", "get_project")
        raise AssertionError("expected HTTPException for cross-project access")
    except HTTPException as exc:
        assert exc.status_code == 403


def test_guard_blocks_missing_perm():
    key = _make_key(project_id="pA", scopes=["topology:read"])
    try:
        _run_guard(key, "pA", "vm_exec")
        raise AssertionError("expected HTTPException for missing permission")
    except HTTPException as exc:
        assert exc.status_code == 403


# ---------------------------------------------------------------------------
# Integration tests — real HTTP requests with Bearer trk_ headers
# ---------------------------------------------------------------------------
def _make_owner():
    db = TestSession()
    user = User(
        id=str(uuid.uuid4()),
        email=f"owner-{uuid.uuid4().hex[:8]}@troshka",
        display_name="owner",
        role="user",
        auth_source="sso",
    )
    db.add(user)
    db.commit()
    uid = user.id
    db.close()
    return uid


def _make_project(owner_id, state="draft", **kwargs):
    db = TestSession()
    p = Project(
        id=str(uuid.uuid4()),
        name=f"proj-{uuid.uuid4().hex[:8]}",
        state=state,
        owner_id=owner_id,
        **kwargs,
    )
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    return pid


def _make_db_key(owner_id, project_id=None, scopes=None):
    """Persist an ApiKey and return the RAW bearer token."""
    raw = generate_api_key()
    db = TestSession()
    key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=owner_id,
        name="ops-pod",
        key_hash=hash_key(raw),
        key_prefix=raw[:10],
        is_active=True,
        project_id=project_id,
        scopes=scopes,
    )
    db.add(key)
    db.commit()
    db.close()
    return raw


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_scoped_key_can_read_its_project():
    owner = _make_owner()
    pid = _make_project(owner)
    raw = _make_db_key(owner, project_id=pid, scopes=["topology:read"])
    resp = client.get(f"/api/v1/projects/{pid}", headers=_auth(raw))
    assert resp.status_code == 200
    assert resp.json()["id"] == pid


def test_scoped_key_cannot_read_other_project():
    owner = _make_owner()
    pid_a = _make_project(owner)
    pid_b = _make_project(owner)
    raw = _make_db_key(owner, project_id=pid_a, scopes=["topology:read"])
    resp = client.get(f"/api/v1/projects/{pid_b}", headers=_auth(raw))
    assert resp.status_code == 403


def test_scoped_key_blocked_when_missing_perm():
    """A key scoped to project A but lacking topology:read is rejected on A."""
    owner = _make_owner()
    pid = _make_project(owner)
    raw = _make_db_key(owner, project_id=pid, scopes=["vm:exec"])
    resp = client.get(f"/api/v1/projects/{pid}", headers=_auth(raw))
    assert resp.status_code == 403


def test_scoped_key_without_exec_perm_cannot_exec():
    owner = _make_owner()
    pid = _make_project(owner, state="active")
    raw = _make_db_key(owner, project_id=pid, scopes=["topology:read"])
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/vm-1/exec",
        headers=_auth(raw),
        json={"command": "echo hi"},
    )
    assert resp.status_code == 403


def test_scoped_key_with_exec_perm_passes_scope_check():
    """With vm:exec granted, the scope gate passes; failure is downstream (no host)."""
    owner = _make_owner()
    pid = _make_project(owner, state="active")
    raw = _make_db_key(owner, project_id=pid, scopes=["topology:read", "vm:exec"])
    resp = client.post(
        f"/api/v1/projects/{pid}/vms/vm-1/exec",
        headers=_auth(raw),
        json={"command": "echo hi"},
    )
    # Scope gate passed; body fails on missing host (503), NOT a scope 403.
    assert resp.status_code != 403


def test_scoped_key_default_denied_on_unlisted_routes():
    """Default-deny: a scoped key is 403 on any route not in the allowlist,
    even routes its owner would normally pass (kubeconfig exfil, delete, deploy).
    """
    owner = _make_owner()
    pid = _make_project(owner, state="active")
    # Grant BOTH allowlisted perms to prove the block is route-based, not perm-based.
    raw = _make_db_key(owner, project_id=pid, scopes=["topology:read", "vm:exec"])
    h = _auth(raw)
    assert (
        client.get(f"/api/v1/projects/{pid}/kubeconfig", headers=h).status_code == 403
    )
    assert client.delete(f"/api/v1/projects/{pid}", headers=h).status_code == 403
    assert (
        client.post(f"/api/v1/projects/{pid}/deploy", headers=h, json={}).status_code
        == 403
    )
    # Sanity: the allowlisted read still works for the same key.
    assert client.get(f"/api/v1/projects/{pid}", headers=h).status_code == 200


def test_scoped_key_default_denied_on_sibling_routers():
    """The guard covers sibling routers mounted under /projects/{id}, not just
    the projects router: vms, networks, disks, eips, portal-token all 403.
    """
    owner = _make_owner()
    pid = _make_project(owner, state="active")
    raw = _make_db_key(owner, project_id=pid, scopes=["topology:read", "vm:exec"])
    h = _auth(raw)
    assert client.get(f"/api/v1/projects/{pid}/vms/", headers=h).status_code == 403
    assert client.get(f"/api/v1/projects/{pid}/networks/", headers=h).status_code == 403
    assert client.get(f"/api/v1/projects/{pid}/disks/", headers=h).status_code == 403
    assert (
        client.delete(f"/api/v1/projects/{pid}/vms/vm-1", headers=h).status_code == 403
    )
    assert (
        client.post(
            f"/api/v1/projects/{pid}/portal-token", headers=h, json={}
        ).status_code
        == 403
    )


def test_scoped_key_cannot_reach_other_projects_siblings():
    """Cross-project: scoped key for A is 403 on B's sub-resources too."""
    owner = _make_owner()
    pid_a = _make_project(owner, state="active")
    pid_b = _make_project(owner, state="active")
    raw = _make_db_key(owner, project_id=pid_a, scopes=["topology:read", "vm:exec"])
    resp = client.get(f"/api/v1/projects/{pid_b}/vms/", headers=_auth(raw))
    assert resp.status_code == 403


def test_unscoped_key_reaches_sibling_routers():
    """Unscoped keys are unaffected by the guard on sibling routers."""
    owner = _make_owner()
    pid = _make_project(owner)
    raw = _make_db_key(owner, project_id=None, scopes=None)
    h = _auth(raw)
    assert client.get(f"/api/v1/projects/{pid}/vms/", headers=h).status_code == 200
    assert client.get(f"/api/v1/projects/{pid}/networks/", headers=h).status_code == 200


def test_scoped_key_cannot_open_websocket():
    """Websockets bypass HTTP deps, so /ws rejects scoped keys inline (4003)."""
    owner = _make_owner()
    pid = _make_project(owner, state="active")
    raw = _make_db_key(owner, project_id=pid, scopes=["topology:read", "vm:exec"])
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/v1/projects/{pid}/ws?token={raw}") as ws:
            ws.receive_json()
    assert exc.value.code == 4003


def test_unscoped_key_has_full_access():
    """An unscoped key behaves exactly as today: full access to the owner's projects."""
    owner = _make_owner()
    pid_a = _make_project(owner)
    pid_b = _make_project(owner)
    raw = _make_db_key(owner, project_id=None, scopes=None)
    assert (
        client.get(f"/api/v1/projects/{pid_a}", headers=_auth(raw)).status_code == 200
    )
    assert (
        client.get(f"/api/v1/projects/{pid_b}", headers=_auth(raw)).status_code == 200
    )
    # Unscoped key reaches an un-allowlisted route unchanged (kubeconfig → not 403).
    assert (
        client.get(
            f"/api/v1/projects/{pid_a}/kubeconfig", headers=_auth(raw)
        ).status_code
        != 403
    )
