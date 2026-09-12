from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.projects import get_cluster_access
from app.core.auth import hash_password
from app.core.database import get_db
from app.main import app
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _project(db):
    user = User(
        email="test2@example.com",
        display_name="Test2",
        role="user",
        auth_source="local",
        password_hash=hash_password("pass"),
    )
    db.add(user)
    db.flush()
    prov = Provider(name="p2", type="troshka", credentials="{}")
    db.add(prov)
    db.flush()
    proj = Project(name="ca-ep", owner_id=user.id, provider_id=prov.id)
    db.add(proj)
    db.commit()
    return proj


def test_get_cluster_access_returns_clusters():
    db = TestSession()
    proj = _project(db)
    pid = proj.id
    db.close()
    with patch(
        "app.api.projects.resolve_cluster_access",
        return_value={"cl-1": {"api_url": "https://x:6443", "api_token": "t"}},
    ):
        # dev mode auto-authenticates as admin
        resp = client.get(f"/api/v1/projects/{pid}/cluster-access")
    assert resp.status_code == 200
    assert resp.json()["clusters"]["cl-1"]["api_token"] == "t"


def test_get_cluster_access_denies_non_owner():
    db = TestSession()
    owner = User(
        email="owner@example.com",
        display_name="Owner",
        role="user",
        auth_source="local",
        password_hash=hash_password("pass"),
    )
    db.add(owner)
    db.flush()
    non_owner = User(
        email="intruder@example.com",
        display_name="Intruder",
        role="user",
        auth_source="local",
        password_hash=hash_password("pass"),
    )
    db.add(non_owner)
    db.flush()
    prov = Provider(name="p-deny", type="troshka", credentials="{}")
    db.add(prov)
    db.flush()
    proj = Project(name="deny-test", owner_id=owner.id, provider_id=prov.id)
    db.add(proj)
    db.commit()
    with pytest.raises(HTTPException) as ei:
        get_cluster_access(proj.id, non_owner, db)
    assert ei.value.status_code == 403
    db.close()
