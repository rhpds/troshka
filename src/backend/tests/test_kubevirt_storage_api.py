"""Native usage authorization and non-disclosing scope projections."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_user
from app.core.database import get_db
from app.main import app
from app.models.managed_group import ManagedGroup, ManagedGroupManager
from app.models.project import Project, ProjectShare
from app.models.user import User
from tests.test_kubevirt_storage_reports import ingest
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture


@pytest.fixture
def native_client(native_case, monkeypatch):
    from app.api import quotas

    case = native_case
    previous = dict(app.dependency_overrides)

    def database():
        with case.sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(
        quotas,
        "datetime",
        SimpleNamespace(
            now=lambda zone: case.report.completed_at + timedelta(seconds=2)
        ),
    )
    ingest(case)

    def forbidden(*args, **kwargs):
        raise AssertionError("native usage invoked external collection or credentials")

    from app.models.provider import Provider
    from app.services import kubevirt_storage_collector

    monkeypatch.setattr(kubevirt_storage_collector, "collect_report", forbidden)
    monkeypatch.setattr(Provider, "get_credentials", forbidden)
    yield TestClient(app), case
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def act(case, user_id):
    with case.sessions() as db:
        user = db.get(User, user_id)
        db.expunge(user)
    app.dependency_overrides[get_current_user] = lambda: user


def url(scope, identity):
    return f"/api/v1/quotas/{scope}/{identity}/native-storage"


def test_owner_shared_viewer_admin_and_no_graph_or_pool_leaks(native_client):
    client, q = native_client
    pid = str(q.report.project_roots[0].project_id)
    act(q, q.owner_id)
    response = client.get(url("project", pid))
    assert response.status_code == 200
    assert response.json()["allocated_bytes"] == 30
    assert response.json()["enforcement_eligible"] is False
    assert not (
        {
            "platform_bytes",
            "unattributed_retained_projects",
            "unattributed_retained_known_bytes",
        }
        & response.json().keys()
    )
    for private in [
        "child-1",
        "parent-1",
        "cache-ns",
        "pool_id",
        str(q.report.project_roots[1].project_id),
    ]:
        assert private not in response.text
    with q.sessions() as db:
        viewer = User(email="viewer@test.invalid")
        db.add(viewer)
        db.flush()
        viewer_id = viewer.id
        db.add(ProjectShare(project_id=pid, user_id=viewer_id, permission="view"))
        db.commit()
    act(q, viewer_id)
    assert client.get(url("project", pid)).status_code == 200
    assert client.get(url("user", q.owner_id)).status_code == 403
    assert (
        client.get(
            url("project", str(q.report.project_roots[1].project_id))
        ).status_code
        == 403
    )
    act(q, q.admin_id)
    assert client.get(url("project", pid)).json()["platform_bytes"] == 100


def test_transfer_and_membership_revocation_use_current_database(native_client):
    client, q = native_client
    with q.sessions() as db:
        org = ManagedGroup(
            name="Native org", normalized_name="native-org", is_organization=True
        )
        manager = User(email="native-manager@test.invalid")
        db.add_all([org, manager])
        db.flush()
        org_id, manager_id = org.id, manager.id
        db.get(User, q.owner_id).organization_id = org_id
        db.add(ManagedGroupManager(group_id=org_id, user_id=manager_id))
        db.commit()
    act(q, manager_id)
    assert client.get(url("organization", org_id)).json()["allocated_bytes"] == 80
    assert client.get(url("user", q.owner_id)).status_code == 200
    with q.sessions() as db:
        db.get(User, q.owner_id).organization_id = None
        db.commit()
    assert client.get(url("user", q.owner_id)).status_code == 403
    with q.sessions() as db:
        db.query(ManagedGroupManager).filter_by(user_id=manager_id).delete()
        db.commit()
    assert client.get(url("organization", org_id)).status_code == 403
    pid = str(q.report.project_roots[0].project_id)
    with q.sessions() as db:
        db.get(Project, pid).owner_id = manager_id
        db.commit()
    act(q, q.owner_id)
    assert client.get(url("project", pid)).status_code == 403
    assert client.get(url("user", q.owner_id)).json()["allocated_bytes"] == 50
    act(q, manager_id)
    assert client.get(url("project", pid)).json()["allocated_bytes"] == 30


def test_deleted_roots_are_admin_diagnostics_and_never_user_zero(native_client):
    client, q = native_client
    pid = str(q.report.project_roots[0].project_id)
    with q.sessions() as db:
        db.query(Project).filter_by(id=pid).delete()
        db.commit()
    act(q, q.owner_id)
    assert client.get(url("project", pid)).status_code == 404
    body = client.get(url("user", q.owner_id)).json()
    assert body["allocated_bytes"] is None
    assert body["known_allocated_bytes"] == 50
    assert "retained_attribution_unavailable" in body["reason_codes"]
    assert "unattributed_retained_projects" not in body
    act(q, q.admin_id)
    body = client.get(url("user", q.owner_id)).json()
    assert body["unattributed_retained_projects"] == 1
    assert body["unattributed_retained_known_bytes"] == 30


def test_new_native_placement_is_missing_coverage_and_drafts_are_not_zero(
    native_client,
):
    client, q = native_client
    with q.sessions() as db:
        first = db.get(Project, str(q.report.project_roots[0].project_id))
        db.add(
            Project(name="native pending", owner_id=q.owner_id, host_id=first.host_id)
        )
        draft = Project(name="unplaced draft", owner_id=q.owner_id)
        db.add(draft)
        db.commit()
        draft_id = draft.id
    act(q, q.owner_id)
    body = client.get(url("user", q.owner_id)).json()
    assert body["expected_projects"] == 3
    assert body["covered_projects"] == 2
    assert body["allocated_bytes"] is None
    assert body["known_allocated_bytes"] == 80
    assert client.get(url("project", draft_id)).json()["allocated_bytes"] is None


def test_current_role_is_reloaded_even_when_auth_dependency_has_stale_admin(
    native_client,
):
    client, q = native_client
    act(q, q.admin_id)
    with q.sessions() as db:
        db.get(User, q.admin_id).role = "user"
        db.commit()
    assert client.get(url("user", q.owner_id)).status_code == 403


def test_api_preserves_exact_large_integer_tokens(native_client):
    from tests.test_kubevirt_storage_reports import revised

    client, q = native_client
    large = 2**53 + 1
    images = [
        i.model_copy(update={"allocated_bytes": large})
        if i.key.image_id == "child-1"
        else i
        for i in q.report.images
    ]
    report = revised(
        q.report,
        images=images,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
    )
    ingest(q, report)
    act(q, q.owner_id)
    response = client.get(url("project", str(q.report.project_roots[0].project_id)))
    assert response.json()["allocated_bytes"] == large
    assert f'"allocated_bytes":{large}' in response.text


def test_unrecognized_collector_reason_cannot_leak_backend_identity(native_client):
    from app.schemas.kubevirt_storage import Blocker
    from tests.test_kubevirt_storage_reports import revised

    client, q = native_client
    report = revised(
        q.report,
        inventory_blockers=[],
        collection_errors=[Blocker(code="private-backend-image-identifier")],
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
    )
    ingest(q, report)
    act(q, q.owner_id)
    response = client.get(url("user", q.owner_id))
    assert "private-backend-image-identifier" not in response.text
    assert "collection_incomplete" in response.json()["reason_codes"]
