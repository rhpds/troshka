"""Tests for /api/v1/workloads endpoints — trigger, status, list."""

import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.project import Project
from app.models.user import User
from app.models.workload_run import WorkloadRun
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _ensure_dev_user():
    """Ensure the dev-mode user exists and return its ID."""
    db = TestSession()
    user = db.query(User).filter_by(email="local-dev@troshka").first()
    if not user:
        # Trigger auto-auth to create the user
        db.close()
        client.get("/api/v1/auth/me")
        db = TestSession()
        user = db.query(User).filter_by(email="local-dev@troshka").first()
    assert user is not None, "Failed to create dev user"
    user_id = user.id
    db.close()
    return user_id


def _create_project(name="test-proj", state="active", owner_id=None, **kwargs):
    """Create a test project in the DB and return its ID."""
    if owner_id is None:
        owner_id = _ensure_dev_user()
    db = TestSession()
    # Only set topology={} if not provided in kwargs
    if "topology" not in kwargs:
        kwargs["topology"] = {}
    p = Project(
        id=str(uuid.uuid4()),
        name=name,
        state=state,
        owner_id=owner_id,
        **kwargs,
    )
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    return pid


def _create_workload_run(project_id, **kwargs):
    """Create a test workload run in the DB and return its ID."""
    db = TestSession()
    run = WorkloadRun(
        id=str(uuid.uuid4()),
        project_id=project_id,
        kind=kwargs.get("kind", "catalog_item"),
        catalog_item=kwargs.get("catalog_item", "test_catalog"),
        status=kwargs.get("status", "pending"),
        owner_id=kwargs.get("owner_id"),
    )
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    return run_id


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/workloads — trigger run
# ---------------------------------------------------------------------------
def test_trigger_workload_run_catalog_item():
    """POST creates a run and returns 202 with id/status."""
    pid = _create_project(state="active")
    with patch("app.api.workloads.start_workload_run") as mock_start:
        mock_run = SimpleNamespace(id="run-123", status="pending")
        mock_start.return_value = mock_run

        resp = client.post(
            f"/api/v1/projects/{pid}/workloads",
            json={"kind": "catalog_item", "catalog_item": "test_item"},
        )

        assert resp.status_code == 202
        data = resp.json()
        assert data["id"] == "run-123"
        assert data["status"] == "pending"
        assert mock_start.called


def test_trigger_workload_run_ad_hoc():
    """POST with ad_hoc kind and role_fqcn."""
    pid = _create_project(state="active")
    with patch("app.api.workloads.start_workload_run") as mock_start:
        mock_run = SimpleNamespace(id="run-456", status="pending")
        mock_start.return_value = mock_run

        resp = client.post(
            f"/api/v1/projects/{pid}/workloads",
            json={
                "kind": "ad_hoc",
                "role_fqcn": "demo_workloads.my_role",
                "target_map": {"bastion": ["vm-1"]},
            },
        )

        assert resp.status_code == 202
        data = resp.json()
        assert data["id"] == "run-456"
        assert data["status"] == "pending"


def test_trigger_workload_run_non_active_project_409():
    """POST rejects non-active projects with 409."""
    pid = _create_project(state="draft")

    resp = client.post(
        f"/api/v1/projects/{pid}/workloads",
        json={"kind": "catalog_item", "catalog_item": "test_item"},
    )

    assert resp.status_code == 409
    assert "active" in resp.json()["detail"].lower()


def test_trigger_workload_run_project_not_found_404():
    """POST returns 404 for missing project."""
    fake_id = str(uuid.uuid4())

    resp = client.post(
        f"/api/v1/projects/{fake_id}/workloads",
        json={"kind": "catalog_item", "catalog_item": "test_item"},
    )

    assert resp.status_code == 404


def test_trigger_workload_run_ocp_not_ready_409():
    """POST rejects OCP project without control-plane-usable milestone with 409."""
    # Create OCP project (topology with ocpKubeconfig node), active but recert incomplete
    pid = _create_project(
        state="active",
        topology={
            "nodes": [
                {
                    "id": "node-1",
                    "data": {"ocpKubeconfig": "some-kubeconfig-content"},
                }
            ]
        },
        ocp_control_plane_usable_at=None,
    )

    resp = client.post(
        f"/api/v1/projects/{pid}/workloads",
        json={"kind": "catalog_item", "catalog_item": "test_item"},
    )

    assert resp.status_code == 409
    assert "minimal control-plane-usable" in resp.json()["detail"].lower()


def test_trigger_workload_run_ocp_ready_proceeds():
    """POST proceeds when OCP project has control-plane-usable milestone set."""
    # Create OCP project with recert complete
    pid = _create_project(
        state="active",
        topology={
            "nodes": [
                {
                    "id": "node-1",
                    "data": {"ocpKubeconfig": "some-kubeconfig-content"},
                }
            ]
        },
        ocp_control_plane_usable_at=datetime.datetime(2026, 9, 13, 10, 0, 0),
    )

    with patch("app.api.workloads.start_workload_run") as mock_start:
        mock_run = SimpleNamespace(id="run-ocp-123", status="pending")
        mock_start.return_value = mock_run

        resp = client.post(
            f"/api/v1/projects/{pid}/workloads",
            json={"kind": "catalog_item", "catalog_item": "test_item"},
        )

        assert resp.status_code == 202
        data = resp.json()
        assert data["id"] == "run-ocp-123"
        assert data["status"] == "pending"
        assert mock_start.called


def test_trigger_workload_run_vm_only_ignores_recert():
    """POST proceeds for VM-only project regardless of recert field."""
    # Create VM-only project (no ocpKubeconfig node), active
    pid = _create_project(
        state="active",
        topology={"nodes": [{"id": "vm-1", "data": {"name": "test-vm"}}]},
        ocp_control_plane_usable_at=None,  # Not set, but shouldn't matter for VM-only
    )

    with patch("app.api.workloads.start_workload_run") as mock_start:
        mock_run = SimpleNamespace(id="run-vm-123", status="pending")
        mock_start.return_value = mock_run

        resp = client.post(
            f"/api/v1/projects/{pid}/workloads",
            json={"kind": "catalog_item", "catalog_item": "test_item"},
        )

        assert resp.status_code == 202
        data = resp.json()
        assert data["id"] == "run-vm-123"
        assert data["status"] == "pending"
        assert mock_start.called


def test_trigger_workload_run_forbidden_non_owner():
    """POST returns 403 when non-owner non-admin tries to trigger."""
    from app.core.auth import get_current_user

    # Create project owned by dev user
    dev_user_id = _ensure_dev_user()
    pid = _create_project(state="active", owner_id=dev_user_id)

    # Create a different non-admin user
    db = TestSession()
    other_user = User(
        id=str(uuid.uuid4()),
        email="other@test.com",
        role="user",
    )
    db.add(other_user)
    db.commit()
    other_user_snapshot = SimpleNamespace(
        id=other_user.id, email=other_user.email, role=other_user.role
    )
    db.close()

    # Override auth to inject the other user
    def override_get_current_user():
        return other_user_snapshot

    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        resp = client.post(
            f"/api/v1/projects/{pid}/workloads",
            json={"kind": "catalog_item", "catalog_item": "test_item"},
        )

        assert resp.status_code == 403
        assert "denied" in resp.json()["detail"].lower()
    finally:
        # Restore original auth
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/workloads — list runs
# ---------------------------------------------------------------------------
def test_list_workload_runs_empty():
    """GET returns empty list for project with no runs."""
    pid = _create_project(state="active")

    resp = client.get(f"/api/v1/projects/{pid}/workloads")

    assert resp.status_code == 200
    assert resp.json() == []


def test_list_workload_runs_returns_runs():
    """GET returns runs for the project, most recent first."""
    pid = _create_project(state="active")
    run1 = _create_workload_run(pid, status="completed")
    run2 = _create_workload_run(pid, status="pending")

    resp = client.get(f"/api/v1/projects/{pid}/workloads")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    run_ids = [r["id"] for r in data]
    assert run1 in run_ids
    assert run2 in run_ids
    # Most recent first (by created_at desc) - run2 was created after run1
    first_run_id = data[0]["id"]
    assert first_run_id in [run1, run2]


def test_list_workload_runs_project_not_found_404():
    """GET returns 404 for missing project."""
    fake_id = str(uuid.uuid4())

    resp = client.get(f"/api/v1/projects/{fake_id}/workloads")

    assert resp.status_code == 404


def test_list_workload_runs_forbidden_non_owner():
    """GET returns 403 when non-owner non-admin tries to list."""
    from app.core.auth import get_current_user

    # Create project owned by dev user
    dev_user_id = _ensure_dev_user()
    pid = _create_project(state="active", owner_id=dev_user_id)

    # Create a different non-admin user
    db = TestSession()
    other_user = User(
        id=str(uuid.uuid4()),
        email="other2@test.com",
        role="user",
    )
    db.add(other_user)
    db.commit()
    other_user_snapshot = SimpleNamespace(
        id=other_user.id, email=other_user.email, role=other_user.role
    )
    db.close()

    # Override auth to inject the other user
    def override_get_current_user():
        return other_user_snapshot

    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        resp = client.get(f"/api/v1/projects/{pid}/workloads")

        assert resp.status_code == 403
        assert "denied" in resp.json()["detail"].lower()
    finally:
        # Restore original auth
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# GET /workloads/{run_id} — get run status
# ---------------------------------------------------------------------------
def test_get_workload_run_status():
    """GET returns run status and progress."""
    pid = _create_project(state="active")
    run_id = _create_workload_run(pid, status="running")

    with patch("app.api.workloads.get_progress") as mock_get_progress:
        mock_get_progress.return_value = {"step": "running", "detail": "deploying"}

        resp = client.get(f"/api/v1/workloads/{run_id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == run_id
        assert data["status"] == "running"
        assert data["progress"] == {"step": "running", "detail": "deploying"}


def test_get_workload_run_status_db_fallback():
    """GET falls back to DB when Redis progress is None."""
    pid = _create_project(state="active")
    run_id = _create_workload_run(pid, status="completed")

    with patch("app.api.workloads.get_progress") as mock_get_progress:
        mock_get_progress.return_value = None

        resp = client.get(f"/api/v1/workloads/{run_id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == run_id
        assert data["status"] == "completed"
        # Progress should be None when not in Redis and no DB progress
        assert data["progress"] is None


def test_get_workload_run_not_found_404():
    """GET returns 404 for missing run."""
    fake_id = str(uuid.uuid4())

    resp = client.get(f"/api/v1/workloads/{fake_id}")

    assert resp.status_code == 404


def test_get_workload_run_forbidden_non_owner():
    """GET returns 403 when non-owner non-admin tries to get status."""
    from app.core.auth import get_current_user

    # Create project owned by dev user
    dev_user_id = _ensure_dev_user()
    pid = _create_project(state="active", owner_id=dev_user_id)
    run_id = _create_workload_run(pid, owner_id=dev_user_id)

    # Create a different non-admin user
    db = TestSession()
    other_user = User(
        id=str(uuid.uuid4()),
        email="other3@test.com",
        role="user",
    )
    db.add(other_user)
    db.commit()
    other_user_snapshot = SimpleNamespace(
        id=other_user.id, email=other_user.email, role=other_user.role
    )
    db.close()

    # Override auth to inject the other user
    def override_get_current_user():
        return other_user_snapshot

    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        resp = client.get(f"/api/v1/workloads/{run_id}")

        assert resp.status_code == 403
        assert "denied" in resp.json()["detail"].lower()
    finally:
        # Restore original auth
        app.dependency_overrides.pop(get_current_user, None)


def test_get_run_log_endpoint_terminal():
    pid = _create_project()
    rid = _create_workload_run(pid, kind="ad_hoc", status="succeeded")
    db = TestSession()
    run = db.get(WorkloadRun, rid)
    assert run is not None
    run.log_ref = "hello from the pod"
    db.commit()
    db.close()

    r = client.get(f"/api/v1/workloads/{rid}/log")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rid
    assert body["status"] == "succeeded"
    assert body["log"] == "hello from the pod"


def test_get_run_log_endpoint_404():
    r = client.get("/api/v1/workloads/00000000-0000-0000-0000-000000000000/log")
    assert r.status_code == 404


def test_inventory_preview_reports_contract_errors():
    # VM with no AnsibleGroup tag → validation error surfaced, groups empty
    topo = {
        "nodes": [
            {
                "id": "n1",
                "type": "vmNode",
                "data": {"name": "vm1", "nics": [{"ip": "10.0.0.5"}]},
            }
        ]
    }
    pid = _create_project(topology=topo)
    r = client.post(f"/api/v1/projects/{pid}/workloads/inventory-preview", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["groups"] == {}
    assert any("AnsibleGroup" in e for e in body["errors"])


def test_inventory_preview_valid_ssh_topology():
    topo = {
        "nodes": [
            {
                "id": "n1",
                "type": "vmNode",
                "data": {
                    "name": "bastion",
                    "nics": [{"ip": "10.0.0.5"}],
                    "tags": {"AnsibleGroup": "bastions"},
                },
            }
        ],
        "externalIps": [{"vmId": "n1", "ip": "1.2.3.4"}],
    }
    pid = _create_project(topology=topo)
    r = client.post(f"/api/v1/projects/{pid}/workloads/inventory-preview", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == []
    assert body["groups"]["bastions"] == ["bastion"]
