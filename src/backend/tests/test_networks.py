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
    email="net-test@example.com",
    display_name="Test",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PROJECT_ID = None


def test_setup():
    global PROJECT_ID
    resp = client.post("/api/v1/projects", json={"name": "Net Test"}, headers=HEADERS)
    PROJECT_ID = resp.json()["id"]


def test_create_network():
    resp = client.post(
        f"/api/v1/projects/{PROJECT_ID}/networks",
        json={
            "name": "lab-net",
            "cidr": "10.0.1.0/24",
            "dhcp_enabled": True,
            "dns_enabled": True,
            "dns_domain": "lab.local",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["cidr"] == "10.0.1.0/24"
    assert resp.json()["dhcp_enabled"] is True


def test_list_networks():
    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/networks", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_delete_network():
    list_resp = client.get(f"/api/v1/projects/{PROJECT_ID}/networks", headers=HEADERS)
    net_id = list_resp.json()[0]["id"]
    resp = client.delete(
        f"/api/v1/projects/{PROJECT_ID}/networks/{net_id}", headers=HEADERS
    )
    assert resp.status_code == 204
