"""Native usage authorization and non-disclosing scope projections."""

import json
from datetime import timedelta
from pathlib import Path
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
def unimported_native_client(native_case, monkeypatch):
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

    def forbidden(*args, **kwargs):
        raise AssertionError("native usage invoked external collection or credentials")

    from app.models.provider import Provider
    from app.services import kubevirt_storage_collector

    monkeypatch.setattr(kubevirt_storage_collector, "collect_report", forbidden)
    monkeypatch.setattr(Provider, "get_credentials", forbidden)
    yield TestClient(app), case
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.fixture
def native_client(unimported_native_client):
    client, case = unimported_native_client
    ingest(case)
    return client, case


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


def test_importer_api_ui_replay_and_transfer_keep_original_evidence(native_client):
    """Replay sanitized child → private zero parent → golden through real SQLite."""
    from app.models.kubevirt_storage import NativeStorageSnapshot
    from app.models.quota import QuotaReadiness, QuotaReservation

    client, q = native_client
    pid = str(q.report.project_roots[0].project_id)
    fixture = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/lib/__fixtures__/native-storage-replay.json"
    )
    act(q, q.owner_id)
    assert client.get(url("project", pid)).json() == json.loads(fixture.read_text())
    with q.sessions() as db:
        before = {row.id: row.payload for row in db.query(NativeStorageSnapshot)}
        org = ManagedGroup(
            name="Replay org", normalized_name="replay-org", is_organization=True
        )
        db.add(org)
        db.flush()
        org_id = org.id
        db.get(User, q.owner_id).organization_id = org_id
        db.get(User, q.admin_id).organization_id = org_id
        db.commit()

    act(q, q.admin_id)
    organization = client.get(url("organization", org_id)).json()
    assert organization["allocated_bytes"] == 80
    assert organization["platform_bytes"] == 100  # one cache, never per project
    assert organization["covered_projects"] == 2
    with q.sessions() as db:
        db.get(Project, pid).owner_id = q.admin_id
        db.commit()
    assert client.get(url("user", q.owner_id)).json()["allocated_bytes"] == 50
    assert client.get(url("user", q.admin_id)).json()["allocated_bytes"] == 30
    assert client.get(url("organization", org_id)).json()["allocated_bytes"] == 80
    with q.sessions() as db:
        assert {
            row.id: row.payload for row in db.query(NativeStorageSnapshot)
        } == before
        assert db.get(QuotaReadiness, "baseline").ready is False
        assert db.query(QuotaReservation).count() == 0


@pytest.mark.parametrize(
    "code,known,status",
    [
        ("backend_identity_changed", 0, "unavailable"),
        ("backend_identity_recheck_unavailable", 0, "unavailable"),
        ("backend_identity_or_capability_unavailable", 0, "unavailable"),
        ("inventory_changed", 30, "partial"),
    ],
)
def test_import_and_api_recompute_identity_failure_vs_valid_subset(
    native_client, code, known, status
):
    from app.models.kubevirt_storage import NativeStorageSnapshot
    from app.schemas.kubevirt_storage import Blocker
    from tests.test_kubevirt_storage_reports import revised

    client, q = native_client
    report = revised(
        q.report,
        inventory_blockers=[],
        collection_errors=[Blocker(code=code)],
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
    )
    # Supplied positive complete totals are assertions, not importer authority.
    assert report.projects[0].known_allocated_bytes == 30
    identity = ingest(q, report)
    with q.sessions() as db:
        saved = db.get(NativeStorageSnapshot, identity)
        assert saved.accepted is True
        assert saved.payload == report.model_dump(mode="json")
    act(q, q.owner_id)
    pid = str(report.project_roots[0].project_id)
    response = client.get(url("project", pid))
    assert response.status_code == 200
    assert response.json()["allocated_bytes"] is None
    assert response.json()["known_allocated_bytes"] == known
    assert response.json()["status"] == status
    assert response.json()["covered_projects"] == 0
    assert code in response.json()["reason_codes"]
    assert response.json()["enforcement_eligible"] is False


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize(
    "tampered_field,replacement_value",
    [
        ("content_uid", "edited-content"),
        ("snapshot_handle_sha256", "f" * 64),
        ("reference_snapshot_uid", "edited-reference"),
    ],
)
def test_discovered_orphan_or_reused_snapshot_never_imports_as_complete(
    native_client, replacement, tampered_field, replacement_value
):
    from app.services.kubevirt_storage_inventory import discover_inventory
    from tests.test_kubevirt_storage_graph import root
    from tests.test_kubevirt_storage_inventory import snapshot_kube
    from tests.test_kubevirt_storage_reports import revised

    client, q = native_client
    kube = snapshot_kube()
    snapshots = kube.snapshots("project-1")
    snapshots[0]["metadata"]["uid"] = "replacement-uid"
    kube.snapshots = lambda ns: snapshots if replacement else []
    discovered = discover_inventory(kube, [], [root(1)])
    assert len(discovered.snapshot_evidence) == 1
    report = revised(
        q.report,
        # Recompute conservatively even if an external report omits its blockers.
        inventory_blockers=[],
        collection_errors=[],
        snapshot_evidence=discovered.snapshot_evidence,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
    )
    ingest(q, report)
    act(q, q.owner_id)
    response = client.get(url("project", str(report.project_roots[0].project_id)))
    assert response.status_code == 200
    assert response.json()["allocated_bytes"] is None
    assert response.json()["status"] == "partial"
    assert response.json()["known_allocated_bytes"] == 30
    assert "csi_snapshot_backend_unresolved" in response.json()["reason_codes"]
    assert "snapshot-uid" not in response.text
    assert "content-uid" not in response.text
    changed = report.snapshot_evidence[0].model_copy(
        update={tampered_field: replacement_value}
    )
    tampered = report.model_copy(update={"snapshot_evidence": [changed]})
    with pytest.raises(ValueError, match="inventory digest mismatch"):
        ingest(q, tampered)
    recomputed = revised(tampered, report_id=report.report_id)
    with pytest.raises(ValueError, match="changed same-ID report replay"):
        ingest(q, recomputed)


@pytest.mark.parametrize("orphan", [True, False])
def test_first_snapshot_partial_import_keeps_measured_api_subsets(
    unimported_native_client, orphan
):
    from app.models.kubevirt_storage import NativeRetainedBinding, NativeStorageSnapshot
    from app.models.quota import QuotaReadiness, QuotaReservation
    from tests.test_kubevirt_storage_reports import discovered_snapshot_report

    client, q = unimported_native_client
    report = discovered_snapshot_report(q, orphan=orphan)
    payload = report.model_dump(mode="json")
    with q.sessions() as db:
        assert db.query(NativeRetainedBinding).count() == 0
        assert db.query(NativeStorageSnapshot).count() == 0
    identity = ingest(q, report)
    with q.sessions() as db:
        saved = db.get(NativeStorageSnapshot, identity)
        assert saved.accepted is True
        assert saved.reason_codes == []
        assert saved.payload == payload
        assert db.query(NativeRetainedBinding).count() == 3
        assert db.get(QuotaReadiness, "baseline").ready is False
        assert db.query(QuotaReservation).count() == 0
    assert report.model_dump(mode="json") == payload
    assert report.snapshot_evidence[0].reference_snapshot_uid == "snapshot-uid"
    assert report.snapshot_evidence[0].snapshot_handle_sha256 is not None
    act(q, q.owner_id)
    affected, unaffected = [str(r.project_id) for r in report.project_roots]
    response = client.get(url("project", affected))
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "partial"
    assert body["allocated_bytes"] is None
    assert body["known_allocated_bytes"] == 30
    assert body["covered_projects"] == 0
    assert body["enforcement_eligible"] is False
    assert {b.code for b in report.inventory_blockers} <= set(body["reason_codes"])
    assert "csi_snapshot_backend_unresolved" in body["reason_codes"]
    control = client.get(url("project", unaffected)).json()
    assert control["status"] == "complete"
    assert control["allocated_bytes"] == control["known_allocated_bytes"] == 50
    assert control["covered_projects"] == 1
    total = client.get(url("user", q.owner_id)).json()
    assert total["allocated_bytes"] is None
    assert total["known_allocated_bytes"] == 80
    assert total["covered_projects"] == 1
