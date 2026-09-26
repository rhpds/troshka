"""import-template cloud-init credential fill-ins (password + SSH key)."""

import uuid

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.project import Project
from app.models.user import User, UserSshKey
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _ensure_dev_user():
    db = TestSession()
    user = db.query(User).filter_by(email="local-dev@troshka").first()
    if not user:
        db.close()
        client.get("/api/v1/auth/me")
        db = TestSession()
        user = db.query(User).filter_by(email="local-dev@troshka").first()
    user_id = user.id
    db.close()
    return user_id


def _create_project(name="test-proj"):
    user_id = _ensure_dev_user()
    db = TestSession()
    p = Project(
        id=str(uuid.uuid4()),
        name=name,
        owner_id=user_id,
        state="draft",
        topology={"nodes": [], "edges": []},
    )
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    return pid


def test_import_template_applies_cloud_init_credentials():
    """common_password + SSH key fill cloud-init VMs that omit them."""
    user_id = _ensure_dev_user()
    db = TestSession()
    key = UserSshKey(
        user_id=user_id,
        name="import-test-key",
        public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIImportTestKey import@test",
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    key_id = key.id
    db.close()

    pid = _create_project(name=f"import-creds-{uuid.uuid4().hex[:8]}")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-template",
        json={
            "common_password": "ConsolePass1",
            "bastion_ssh_key_id": key_id,
            "template_yaml": {
                "vms": {
                    "demo": {
                        "vcpus": 2,
                        "ram_gb": 4,
                        "os": "rhel10",
                        "cloud_init": True,
                        "disks": [{"size_gb": 20}],
                        "nics": [{"network": "mgmt", "ip": "10.0.0.10"}],
                    }
                },
                "networks": {"mgmt": {"cidr": "10.0.0.0/24"}},
            },
        },
    )
    assert resp.status_code == 200
    demo = next(
        n
        for n in resp.json()["topology"]["nodes"]
        if n.get("type") == "vmNode" and n["data"].get("name") == "demo"
    )
    assert demo["data"]["cloudInit"] is True
    assert demo["data"]["ciCloudUserPassword"] == "ConsolePass1"
    assert demo["data"]["ciSshKeyIds"] == [key_id]
    assert demo["data"]["ciSshKeys"] == [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIImportTestKey import@test"
    ]


def test_import_template_does_not_overwrite_yaml_password():
    """YAML cloud_user_password wins over common_password."""
    pid = _create_project(name=f"import-keep-pw-{uuid.uuid4().hex[:8]}")
    resp = client.post(
        f"/api/v1/projects/{pid}/import-template",
        json={
            "common_password": "FromUi",
            "template_yaml": {
                "vms": {
                    "demo": {
                        "vcpus": 2,
                        "ram_gb": 4,
                        "cloud_init": True,
                        "cloud_user_password": "FromYaml",
                        "disks": [{"size_gb": 20}],
                        "nics": [{"network": "mgmt"}],
                    }
                },
                "networks": {"mgmt": {"cidr": "10.0.0.0/24"}},
            },
        },
    )
    assert resp.status_code == 200
    demo = next(
        n
        for n in resp.json()["topology"]["nodes"]
        if n.get("type") == "vmNode" and n["data"].get("name") == "demo"
    )
    assert demo["data"]["ciCloudUserPassword"] == "FromYaml"
