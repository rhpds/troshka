from app.core.auth import hash_password
from app.models.api_key import ApiKey, hash_key
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from app.services.workloads import run_key
from tests.conftest import TestSession


def _project(db):
    user = User(
        email="test@example.com",
        display_name="Test",
        role="user",
        auth_source="local",
        password_hash=hash_password("pass"),
    )
    db.add(user)
    db.flush()
    prov = Provider(name="p", type="troshka", credentials="{}")
    db.add(prov)
    db.flush()
    proj = Project(name="rk", owner_id=user.id, provider_id=prov.id)
    db.add(proj)
    db.commit()
    return proj


def test_mint_run_key_has_expected_scopes():
    db = TestSession()
    try:
        proj = _project(db)
        raw = run_key.mint_run_key(db, proj)
        assert raw.startswith("trk_")
        row = db.query(ApiKey).filter_by(key_hash=hash_key(raw)).one()
        assert row.project_id == proj.id
        assert set(row.scopes or []) == {"topology:read", "vm:exec"}
    finally:
        db.close()
