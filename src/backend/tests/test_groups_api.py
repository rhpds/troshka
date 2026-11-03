"""Managed group authorization through real dependency overrides."""

import uuid
from unittest.mock import patch

import pytest

from app.core.auth import get_current_user
from app.main import app
from app.models.project import Project
from tests.conftest import TestSession
from tests.test_metering_api import (
    _add_invoice,
    _add_statement,
    _add_user,
    _as_user,
    client,
)


@pytest.fixture
def actors():
    suffix = uuid.uuid4().hex
    users = [
        _add_user(f"{name}-{suffix}@test.invalid", role)
        for name, role in [
            ("admin", "admin"),
            ("manager", "user"),
            ("member", "user"),
            ("peer", "user"),
            ("operator", "operator"),
        ]
    ]
    yield users
    app.dependency_overrides.pop(get_current_user, None)


def make_group(admin, manager, member):
    _as_user(admin)
    response = client.post(
        "/api/v1/groups", json={"name": uuid.uuid4().hex, "description": "Team"}
    )
    assert response.status_code == 201
    gid = response.json()["id"]
    assert (
        client.post(
            f"/api/v1/groups/{gid}/managers", json={"email": manager.email}
        ).status_code
        == 200
    )
    assert client.put(f"/api/v1/groups/{gid}/members/{member.id}").status_code == 200
    return gid


def test_group_access_delegation_and_revocation(actors):
    admin, manager, member, peer, operator = actors
    gid = make_group(admin, manager, member)
    other = make_group(admin, peer, peer)
    invoice = _add_invoice(member.id, "Member lab")
    statement = _add_statement(member.id)
    ownerless = _add_invoice(None, "Orphan")
    _as_user(manager)
    assert client.get(f"/api/v1/groups/{other}").status_code == 404
    assert (
        client.delete(f"/api/v1/groups/{gid}/managers/{manager.id}").status_code == 409
    )
    assert client.get(f"/api/v1/metering/invoices/{invoice}").status_code == 200
    assert client.get(f"/api/v1/metering/statements/{statement}").status_code == 200
    assert client.get(f"/api/v1/metering/invoices/{ownerless}").status_code == 403
    assert client.put(f"/api/v1/groups/{gid}/members/{peer.id}").status_code == 403
    assert (
        client.patch(f"/api/v1/groups/{gid}", json={"name": "forbidden"}).status_code
        == 403
    )
    assert client.get("/api/v1/users/").status_code == 403
    assert client.put("/api/v1/metering/rates", json={"rates": {}}).status_code == 403
    assert (
        client.post(
            f"/api/v1/groups/{gid}/managers", json={"email": "missing@test.invalid"}
        ).status_code
        == 404
    )
    for _ in range(2):
        assert (
            client.post(
                f"/api/v1/groups/{gid}/managers", json={"email": peer.email}
            ).status_code
            == 200
        )
    assert len(client.get(f"/api/v1/groups/{gid}").json()["managers"]) == 2
    assert (
        client.delete(f"/api/v1/groups/{gid}/managers/{manager.id}").status_code == 200
    )
    assert client.get(f"/api/v1/metering/invoices/{invoice}").status_code == 403
    _as_user(member)
    assert client.get(f"/api/v1/groups/{gid}").status_code == 404
    assert client.get(f"/api/v1/metering/invoices/{invoice}").status_code == 200
    assert (
        client.get(
            f"/api/v1/metering/invoices/{_add_invoice(peer.id, 'Peer')}"
        ).status_code
        == 403
    )
    _as_user(operator)
    assert client.get(f"/api/v1/metering/invoices/{invoice}").status_code == 403


def test_overlapping_groups_project_paths_and_member_revocation(actors):
    admin, manager, member, peer, _ = actors
    first = make_group(admin, manager, member)
    second = make_group(admin, manager, member)
    with TestSession() as db:
        project = Project(name="Scoped", owner_id=member.id, state="draft")
        db.add(project)
        db.commit()
        pid = project.id
    _as_user(manager)
    with patch("app.api.metering.reconcile_project") as reconcile:
        for path in [
            f"/api/v1/metering/projects/{pid}",
            f"/api/v1/projects/{pid}/metering",
        ]:
            assert client.get(path).status_code == 200
        reconcile.assert_not_called()
    assert (
        client.patch(f"/api/v1/projects/{pid}", json={"name": "Forbidden"}).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/metering/groups/{first}/report?year=2026&month=11&owner_id={peer.id}"
        ).status_code
        == 404
    )
    _as_user(admin)
    client.delete(f"/api/v1/groups/{first}/members/{member.id}")
    _as_user(manager)
    assert client.get(f"/api/v1/metering/projects/{pid}").status_code == 200
    _as_user(admin)
    client.delete(f"/api/v1/groups/{second}/members/{member.id}")
    _as_user(manager)
    assert client.get(f"/api/v1/metering/projects/{pid}").status_code == 403


def test_user_deletion_protects_last_manager(actors):
    admin, manager, member, peer, _ = actors
    gid = make_group(admin, manager, member)
    assert client.delete(f"/api/v1/users/{manager.id}").status_code == 409
    client.post(f"/api/v1/groups/{gid}/managers", json={"email": peer.email})
    assert client.delete(f"/api/v1/users/{manager.id}").status_code == 204


def test_admin_crud_duplicates_and_unmanaged_discovery(actors):
    admin, manager, member, peer, _ = actors
    gid = make_group(admin, manager, member)
    original = client.get(f"/api/v1/groups/{gid}").json()
    assert (
        client.post(
            "/api/v1/groups", json={"name": f"  {original['name'].upper()}  "}
        ).status_code
        == 409
    )
    assert client.post("/api/v1/groups", json={"name": "   "}).status_code == 422
    for _ in range(2):
        assert (
            client.put(f"/api/v1/groups/{gid}/members/{member.id}").status_code == 200
        )
    assert len(client.get(f"/api/v1/groups/{gid}").json()["members"]) == 1
    assert client.put(f"/api/v1/groups/{gid}/members/{uuid.uuid4()}").status_code == 404
    assert (
        client.patch(f"/api/v1/groups/{gid}", json={"description": "Changed"}).json()[
            "description"
        ]
        == "Changed"
    )
    _as_user(peer)
    assert client.get("/api/v1/groups").json() == []
    assert client.get("/api/v1/groups/invalid-uuid").status_code == 404
    assert client.delete(f"/api/v1/groups/{gid}").status_code == 403
    _as_user(manager)
    assert gid in {g["id"] for g in client.get("/api/v1/groups").json()}
    _as_user(admin)
    assert (
        client.delete(f"/api/v1/groups/{gid}/managers/{manager.id}").status_code == 409
    )
    assert client.delete(f"/api/v1/groups/{gid}").status_code == 204
    _as_user(manager)
    assert client.get(f"/api/v1/groups/{gid}").status_code == 404


def test_manager_assignment_preserves_exact_email_identity(actors):
    admin, manager, member, _, _ = actors
    gid = make_group(admin, manager, member)
    suffix = uuid.uuid4().hex
    mixed = _add_user(f"Alice-{suffix}@example.com")
    lowercase = _add_user(mixed.email.lower())
    with TestSession() as db:
        db.query(type(mixed)).filter_by(id=mixed.id).update(
            {"auth_source": "openshift"}
        )
        db.commit()
    _as_user(manager)
    assigned = client.post(
        f"/api/v1/groups/{gid}/managers", json={"email": f"  {mixed.email}  "}
    )
    assert assigned.status_code == 200
    ids = {row["id"] for row in assigned.json()["managers"]}
    assert mixed.id in ids
    assert lowercase.id not in ids
    assigned = client.post(
        f"/api/v1/groups/{gid}/managers", json={"email": lowercase.email}
    )
    assert assigned.status_code == 200
    ids = {row["id"] for row in assigned.json()["managers"]}
    assert {mixed.id, lowercase.id}.issubset(ids)
