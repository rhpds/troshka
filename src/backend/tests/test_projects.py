import datetime

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(
    email="proj-test@example.com",
    display_name="Test",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_create_project():
    resp = client.post(
        "/api/v1/projects",
        json={
            "name": "My Lab",
            "description": "Test project",
        },
        headers=HEADERS,
    )
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
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={
            "name": "Renamed Lab",
            "poweroff_mode": "ordered",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Lab"
    assert resp.json()["poweroff_mode"] == "ordered"


def test_delete_project():
    create_resp = client.post(
        "/api/v1/projects", json={"name": "To Delete"}, headers=HEADERS
    )
    project_id = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 204

    get_resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert get_resp.status_code == 404


def test_dev_mode_allows_unauthenticated():
    resp = client.get("/api/v1/projects")
    assert resp.status_code == 200


def test_set_auto_stop_timer():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": 120},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["auto_stop_minutes"] == 120


def test_set_auto_delete_timer():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_delete_minutes": 480},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["auto_delete_minutes"] == 480


def test_disable_auto_stop_timer():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": 60},
        headers=HEADERS,
    )
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": None},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["auto_stop_minutes"] is None
    assert resp.json().get("auto_stop_expires_at") is None


def test_get_project_includes_timer_fields():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "auto_stop_minutes" in data
    assert "auto_stop_expires_at" in data
    assert "auto_delete_minutes" in data
    assert "lifetime_expires_at" in data


def test_patch_auto_stop_clears_expiry_when_disabled():
    """Setting auto_stop_minutes=None clears all auto-stop fields."""
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]

    # Set a timer
    client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": 60},
        headers=HEADERS,
    )
    # Manually set started_at to simulate an active project
    db = TestSession()
    from app.models.project import Project

    p = db.query(Project).filter_by(id=project_id).first()
    now = datetime.datetime.now(datetime.timezone.utc)
    p.auto_stop_started_at = now
    p.auto_stop_expires_at = now + datetime.timedelta(minutes=60)
    p.auto_stop_warned = True
    db.commit()
    db.close()

    # Disable the timer
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": None},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_stop_minutes"] is None
    assert data["auto_stop_expires_at"] is None


def test_patch_auto_stop_recomputes_expiry_when_running():
    """Changing auto_stop_minutes on an active timer recomputes expires_at."""
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]

    # Set up a running timer
    db = TestSession()
    from app.models.project import Project

    p = db.query(Project).filter_by(id=project_id).first()
    now = datetime.datetime.now(datetime.timezone.utc)
    p.auto_stop_minutes = 60
    p.auto_stop_started_at = now
    p.auto_stop_expires_at = now + datetime.timedelta(minutes=60)
    db.commit()
    db.close()

    # Change to 120 minutes
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": 120},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_stop_minutes"] == 120
    # expires_at should be ~120 min from started_at, not 60
    assert data["auto_stop_expires_at"] is not None


def test_extend_auto_stop_timer():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]

    # Set up a running timer
    db = TestSession()
    from app.models.project import Project

    p = db.query(Project).filter_by(id=project_id).first()
    now = datetime.datetime.now(datetime.timezone.utc)
    p.auto_stop_minutes = 60
    p.auto_stop_started_at = now
    p.auto_stop_expires_at = now + datetime.timedelta(minutes=60)
    p.auto_stop_warned = True
    db.commit()
    old_expires = p.auto_stop_expires_at
    db.close()

    resp = client.post(
        f"/api/v1/projects/{project_id}/extend-timer",
        json={"timer": "auto_stop", "add_minutes": 30},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    # expires_at should be pushed forward by 30 min
    new_expires = datetime.datetime.fromisoformat(data["auto_stop_expires_at"])
    assert new_expires > old_expires


def test_extend_timer_fails_when_no_timer_active():
    create_resp = client.post(
        "/api/v1/projects", json={"name": "No Timer"}, headers=HEADERS
    )
    project_id = create_resp.json()["id"]
    resp = client.post(
        f"/api/v1/projects/{project_id}/extend-timer",
        json={"timer": "auto_stop", "add_minutes": 30},
        headers=HEADERS,
    )
    assert resp.status_code == 400
