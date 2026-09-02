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
    email="template-test@example.com",
    display_name="Template Tester",
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


def test_deploy_template_creates_project():
    resp = client.post(
        "/api/v1/deploy-template",
        json={
            "template": "ocp-compact",
            "version": "4.16",
            "name": "My OCP Cluster",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My OCP Cluster"
    assert data["state"] == "draft"
    assert "topology" in data
    nodes = data["topology"]["nodes"]
    vm_nodes = [n for n in nodes if n["type"] == "vmNode"]
    # 3 CP + 1 bastion = 4 VMs
    assert len(vm_nodes) == 4


def test_deploy_template_rejects_unknown_overrides():
    resp = client.post(
        "/api/v1/deploy-template",
        json={
            "template": "ocp-compact",
            "version": "4.16",
            "name": "Custom OCP",
            "overrides": {"control_ram_gb": 32, "worker_count": 2},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "Unknown parameter" in resp.json()["detail"]


def test_deploy_template_rejects_invalid_template():
    resp = client.post(
        "/api/v1/deploy-template",
        json={
            "template": "nonexistent",
            "version": "4.16",
            "name": "Bad Template",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_deploy_template_rejects_invalid_version():
    resp = client.post(
        "/api/v1/deploy-template",
        json={
            "template": "ocp-compact",
            "version": "3.11",
            "name": "Bad Version",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_from_template_persists_install_via_default_pod():
    resp = client.post(
        "/api/v1/projects/from-template",
        json={
            "template_id": "ocp-compact",
            "name": "install-via-default",
            "common_password": "redhat123",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    proj = client.get(f"/api/v1/projects/{pid}", headers=HEADERS).json()
    assert proj["topology"]["ocpInstallVia"] == "pod"


def test_from_template_persists_install_via_bastion():
    resp = client.post(
        "/api/v1/projects/from-template",
        json={
            "template_id": "ocp-compact",
            "name": "install-via-bastion",
            "install_via": "bastion",
            "common_password": "redhat123",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    proj = client.get(f"/api/v1/projects/{pid}", headers=HEADERS).json()
    assert proj["topology"]["ocpInstallVia"] == "bastion"


from pathlib import Path as _Path

import yaml as _yaml

_OCP_TMPL = _Path(__file__).resolve().parents[1] / "templates" / "ocp-compact.yaml"


def _ocp_template_yaml(**extra):
    d = _yaml.safe_load(_OCP_TMPL.read_text())
    d.update(extra)
    return d


def _create_from_template_yaml(name, tmpl):
    resp = client.post(
        "/api/v1/projects/from-template",
        json={"template_yaml": tmpl, "name": name, "common_password": "redhat123"},
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    return client.get(f"/api/v1/projects/{pid}", headers=HEADERS).json()


def test_from_template_yaml_persists_template_install_via_bastion():
    # Template-level install_via must propagate end-to-end (agnosticd path).
    tmpl = _ocp_template_yaml(install_via="bastion")
    proj = _create_from_template_yaml("tmpl-iv-bastion", tmpl)
    assert proj["topology"]["ocpInstallVia"] == "bastion"


def test_from_template_yaml_body_overrides_template_install_via():
    # template says bastion, body says pod -> body wins.
    tmpl = _ocp_template_yaml(install_via="bastion")
    resp = client.post(
        "/api/v1/projects/from-template",
        json={
            "template_yaml": tmpl,
            "name": "tmpl-iv-body-pod",
            "common_password": "redhat123",
            "install_via": "pod",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    proj = client.get(f"/api/v1/projects/{pid}", headers=HEADERS).json()
    assert proj["topology"]["ocpInstallVia"] == "pod"


def test_from_template_yaml_body_overrides_template_install_via_reverse():
    # template says pod, body says bastion -> body wins.
    tmpl = _ocp_template_yaml(install_via="pod")
    resp = client.post(
        "/api/v1/projects/from-template",
        json={
            "template_yaml": tmpl,
            "name": "tmpl-iv-body-bastion",
            "common_password": "redhat123",
            "install_via": "bastion",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    proj = client.get(f"/api/v1/projects/{pid}", headers=HEADERS).json()
    assert proj["topology"]["ocpInstallVia"] == "bastion"
