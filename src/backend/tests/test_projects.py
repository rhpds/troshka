from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(email="proj-test@example.com", display_name="Test", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_create_project():
    resp = client.post("/api/v1/projects", json={
        "name": "My Lab",
        "description": "Test project",
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Lab"
    assert data["owner_id"] == USER_ID
    assert data["state"] == "draft"


def test_list_projects():
    resp = client.get("/api/v1/projects", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["name"] == "My Lab"


def test_get_project():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "My Lab"


def test_update_project():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.patch(f"/api/v1/projects/{project_id}", json={
        "name": "Renamed Lab",
        "poweroff_mode": "ordered",
    }, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Lab"
    assert resp.json()["poweroff_mode"] == "ordered"


def test_delete_project():
    create_resp = client.post("/api/v1/projects", json={"name": "To Delete"}, headers=HEADERS)
    project_id = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 204

    get_resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert get_resp.status_code == 404


def test_unauthorized_access():
    resp = client.get("/api/v1/projects")
    assert resp.status_code == 401
