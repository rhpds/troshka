"""Tests for host API endpoint handlers (provision, install, update, delete, evacuate, storage)."""

import json
import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

# ---------------------------------------------------------------------------
# Module-level fixtures -- capture IDs before closing session
# ---------------------------------------------------------------------------
_db = TestSession()
_admin = User(
    email="hostep-admin@example.com",
    display_name="Host EP Admin",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_admin)
_db.commit()
_db.refresh(_admin)
ADMIN_ID = _admin.id
ADMIN_TOKEN = create_jwt(user_id=_admin.id, email=_admin.email, role=_admin.role)
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

_regular = User(
    email="hostep-user@example.com",
    display_name="Host EP User",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_regular)
_db.commit()
_db.refresh(_regular)
USER_TOKEN = create_jwt(user_id=_regular.id, email=_regular.email, role=_regular.role)
USER_HEADERS = {"Authorization": f"Bearer {USER_TOKEN}"}
_db.close()


def _create_provider(name=None, provider_type="ec2", state="active") -> str:
    """Create a provider and return its ID."""
    db = TestSession()
    prov = Provider(
        id=str(uuid.uuid4()),
        name=name or f"test-prov-{uuid.uuid4().hex[:6]}",
        type=provider_type,
        credentials=json.dumps({"access_key_id": "fake", "secret_access_key": "fake"}),
        default_region="us-east-1",
        default_image="ami-12345678",
        vpc_id="vpc-abc",
        subnet_id="subnet-abc",
        security_group_id="sg-abc",
        state=state,
    )
    db.add(prov)
    db.commit()
    db.refresh(prov)
    pid = prov.id
    db.close()
    return pid


def _create_host(
    provider_id=None,
    state="running",
    agent_status="connected",
    host_type="shared",
    ip_address="10.0.0.99",
    private_key="fake-ssh-key",
    agent_token="fake-token",
    storage_pool_id=None,
) -> str:
    """Create a host and return its ID."""
    db = TestSession()
    h = Host(
        id=str(uuid.uuid4()),
        provider_id=provider_id,
        state=state,
        host_type=host_type,
        ip_address=ip_address,
        private_key=private_key,
        agent_status=agent_status,
        agent_token=agent_token,
        storage_pool_id=storage_pool_id,
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    hid = h.id
    db.close()
    return hid


def _create_pool_with_provider(mode="shared-fsx"):
    """Create a provider and storage pool, return (provider_id, pool_id)."""
    from app.models.storage_pool import StoragePool

    db = TestSession()
    prov = Provider(
        id=str(uuid.uuid4()),
        name=f"pool-prov-{uuid.uuid4().hex[:6]}",
        type="ec2",
        credentials=json.dumps({"access_key_id": "f", "secret_access_key": "f"}),
        default_region="us-east-1",
        state="active",
    )
    db.add(prov)
    db.flush()
    pool = StoragePool(
        id=str(uuid.uuid4()),
        name=f"pool-{uuid.uuid4().hex[:6]}",
        mode=mode,
        status="available",
        provider_id=prov.id,
    )
    db.add(pool)
    db.commit()
    prov_id = prov.id
    pool_id = pool.id
    db.close()
    return prov_id, pool_id


# ===================================================================
# POST /hosts/ (provision)
# ===================================================================


class TestProvisionHost:
    def test_provision_success(self):
        prov_id = _create_provider()

        with patch("app.core.redis.enqueue_job") as mock_enqueue:
            resp = client.post(
                "/api/v1/hosts/",
                json={"provider_id": prov_id, "instance_type": "r8i.4xlarge"},
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["state"] == "provisioning"
        assert data["agent_status"] == "provisioning"
        assert data["provider_id"] == prov_id
        mock_enqueue.assert_called_once()

    def test_provision_provider_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            "/api/v1/hosts/",
            json={"provider_id": fake_id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404
        assert "Provider not found" in resp.json()["detail"]

    def test_provision_provider_not_active(self):
        prov_id = _create_provider(state="disabled")
        resp = client.post(
            "/api/v1/hosts/",
            json={"provider_id": prov_id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "not active" in resp.json()["detail"]

    def test_provision_requires_admin(self):
        prov_id = _create_provider()
        resp = client.post(
            "/api/v1/hosts/",
            json={"provider_id": prov_id},
            headers=USER_HEADERS,
        )
        assert resp.status_code == 403


# ===================================================================
# POST /hosts/{id}/install-agent
# ===================================================================


class TestInstallAgent:
    def test_install_success(self):
        prov_id = _create_provider()
        hid = _create_host(
            provider_id=prov_id,
            agent_status="disconnected",
        )

        with patch("app.core.redis.enqueue_job") as mock_enqueue:
            resp = client.post(
                f"/api/v1/hosts/{hid}/install-agent",
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "installing"
        mock_enqueue.assert_called_once()

    def test_install_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/hosts/{fake_id}/install-agent",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_install_no_ip(self):
        hid = _create_host(ip_address="", agent_status="disconnected")
        resp = client.post(
            f"/api/v1/hosts/{hid}/install-agent",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "no IP" in resp.json()["detail"]

    def test_install_no_ssh_key(self):
        hid = _create_host(private_key="", agent_status="disconnected")
        resp = client.post(
            f"/api/v1/hosts/{hid}/install-agent",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "SSH key" in resp.json()["detail"]

    def test_install_already_in_progress(self):
        hid = _create_host(agent_status="installing")
        resp = client.post(
            f"/api/v1/hosts/{hid}/install-agent",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert "already in progress" in resp.json()["detail"]

    def test_install_requires_admin(self):
        hid = _create_host(agent_status="disconnected")
        resp = client.post(
            f"/api/v1/hosts/{hid}/install-agent",
            headers=USER_HEADERS,
        )
        assert resp.status_code == 403


# ===================================================================
# POST /hosts/{id}/update-agent
# ===================================================================


class TestUpdateAgent:
    def test_update_not_connected(self):
        hid = _create_host(agent_status="disconnected")
        resp = client.post(
            f"/api/v1/hosts/{hid}/update-agent",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "not connected" in resp.json()["detail"]

    def test_update_no_token(self):
        hid = _create_host(agent_status="connected", agent_token="")
        resp = client.post(
            f"/api/v1/hosts/{hid}/update-agent",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "credentials" in resp.json()["detail"]

    def test_update_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/hosts/{fake_id}/update-agent",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_update_requires_admin(self):
        hid = _create_host(agent_status="connected")
        resp = client.post(
            f"/api/v1/hosts/{hid}/update-agent",
            headers=USER_HEADERS,
        )
        assert resp.status_code == 403

    def test_update_with_active_deploy_blocked(self):
        """Agent update should be blocked when deploys are running (without force)."""
        prov_id = _create_provider()
        hid = _create_host(
            provider_id=prov_id,
            agent_status="connected",
            agent_token="tok-123",
        )
        # Create a deploying project on this host
        db = TestSession()
        p = Project(
            name="Deploying Project",
            owner_id=ADMIN_ID,
            state="deploying",
            host_id=hid,
        )
        db.add(p)
        db.commit()
        db.close()

        with patch("os.path.exists", return_value=True), patch(
            "builtins.open",
            return_value=MagicMock(
                __enter__=lambda s: MagicMock(read=lambda: b'VERSION = "dev"'),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            resp = client.post(
                f"/api/v1/hosts/{hid}/update-agent",
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 409
        assert "deploy" in resp.json()["detail"]


# ===================================================================
# DELETE /hosts/{id}
# ===================================================================


class TestRemoveHost:
    def test_delete_host_no_projects(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="running")

        with patch("app.api.hosts._terminate_host_instance"), patch(
            "app.api.hosts._cleanup_console_record"
        ), patch("app.core.redis.enqueue_job"):
            resp = client.delete(
                f"/api/v1/hosts/{hid}",
                headers=ADMIN_HEADERS,
            )
        # 204 for non-ocpvirt hosts
        assert resp.status_code == 204

    def test_delete_host_with_running_projects(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id)

        # Create a running project on this host using ADMIN_ID (not _admin.id)
        db = TestSession()
        p = Project(
            name="Running Project",
            owner_id=ADMIN_ID,
            state="active",
            host_id=hid,
        )
        db.add(p)
        db.commit()
        db.close()

        resp = client.delete(
            f"/api/v1/hosts/{hid}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert "running project" in resp.json()["detail"]

    def test_delete_kubevirt_cluster(self):
        """kubevirt-cluster hosts are deleted immediately.
        The endpoint returns 204 status (from decorator) but the body has JSON."""
        hid = _create_host(host_type="kubevirt-cluster")
        resp = client.delete(
            f"/api/v1/hosts/{hid}",
            headers=ADMIN_HEADERS,
        )
        # FastAPI returns 204 from the decorator but still sends body
        assert resp.status_code == 204

        # Verify the host is actually deleted
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        assert h is None
        db.close()

    def test_delete_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.delete(
            f"/api/v1/hosts/{fake_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_delete_requires_admin(self):
        hid = _create_host()
        resp = client.delete(
            f"/api/v1/hosts/{hid}",
            headers=USER_HEADERS,
        )
        assert resp.status_code == 403


# ===================================================================
# POST /hosts/{id}/evacuate
# ===================================================================


class TestEvacuateHost:
    def _create_pool_and_host(self):
        """Create a shared storage pool, a host in it, and an active project."""
        prov_id, pool_id = _create_pool_with_provider("shared-fsx")

        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="running",
            host_type="shared",
            ip_address="10.0.1.1",
            private_key="key",
            agent_status="connected",
            agent_token="tok",
            storage_pool_id=pool_id,
        )
        db.add(host)
        db.flush()

        proj = Project(
            name="Evacuate Test",
            owner_id=ADMIN_ID,
            state="active",
            host_id=host.id,
        )
        db.add(proj)
        db.commit()
        hid = host.id
        db.close()
        return hid

    def test_evacuate_success(self):
        hid = self._create_pool_and_host()
        with patch("app.services.migration_service.evacuate_host") as mock_evac:
            resp = client.post(
                f"/api/v1/hosts/{hid}/evacuate",
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "evacuating"
        assert data["project_count"] >= 1
        mock_evac.assert_called_once_with(hid)

    def test_evacuate_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/hosts/{fake_id}/evacuate",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_evacuate_no_pool(self):
        hid = _create_host(storage_pool_id=None)
        resp = client.post(
            f"/api/v1/hosts/{hid}/evacuate",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "not in a storage pool" in resp.json()["detail"]

    def test_evacuate_local_pool(self):
        _, pool_id = _create_pool_with_provider("local")

        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="running",
            host_type="shared",
            storage_pool_id=pool_id,
        )
        db.add(host)
        db.commit()
        hid = host.id
        db.close()

        resp = client.post(
            f"/api/v1/hosts/{hid}/evacuate",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "local-mode" in resp.json()["detail"]

    def test_evacuate_no_active_projects(self):
        _, pool_id = _create_pool_with_provider("shared-fsx")

        db = TestSession()
        host = Host(
            id=str(uuid.uuid4()),
            state="running",
            host_type="shared",
            storage_pool_id=pool_id,
        )
        db.add(host)
        db.commit()
        hid = host.id
        db.close()

        resp = client.post(
            f"/api/v1/hosts/{hid}/evacuate",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "No active projects" in resp.json()["detail"]

    def test_evacuate_requires_admin(self):
        hid = _create_host()
        resp = client.post(
            f"/api/v1/hosts/{hid}/evacuate",
            headers=USER_HEADERS,
        )
        assert resp.status_code == 403


# ===================================================================
# GET /hosts/storage
# ===================================================================


class TestHostStorage:
    def test_storage_requires_operator(self):
        resp = client.get("/api/v1/hosts/storage", headers=USER_HEADERS)
        assert resp.status_code == 403


# ===================================================================
# GET /hosts/ (list)
# ===================================================================


class TestListHosts:
    def test_list_returns_200(self):
        """Basic listing returns a JSON list."""
        with patch("app.services.placement.sync_host_capacity"), patch(
            "app.services.eip_service.get_host_eip_usage", return_value=0
        ):
            resp = client.get("/api/v1/hosts/", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_with_region_filter_excludes_unmatched(self):
        """Passing a region query param filters hosts by region."""
        prov_id = _create_provider()
        _create_host(provider_id=prov_id)

        with patch("app.services.placement.sync_host_capacity"), patch(
            "app.services.eip_service.get_host_eip_usage", return_value=0
        ):
            resp = client.get(
                "/api/v1/hosts/",
                params={"region": "ap-southeast-99"},
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        # No hosts should match this fictitious region
        for h in data:
            assert h.get("region") == "ap-southeast-99"

    def test_list_excludes_terminated_hosts(self):
        """Terminated hosts are excluded from listing."""
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="terminated")

        with patch("app.services.placement.sync_host_capacity"), patch(
            "app.services.eip_service.get_host_eip_usage", return_value=0
        ):
            resp = client.get("/api/v1/hosts/", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        ids = [h["id"] for h in resp.json()]
        assert hid not in ids

    def test_list_requires_operator(self):
        """Regular users cannot list hosts."""
        resp = client.get("/api/v1/hosts/", headers=USER_HEADERS)
        assert resp.status_code == 403


# ===================================================================
# GET /hosts/{id} (single host)
# ===================================================================


class TestGetHost:
    def test_get_host_success(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id)
        resp = client.get(f"/api/v1/hosts/{hid}", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["id"] == hid

    def test_get_host_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.get(f"/api/v1/hosts/{fake_id}", headers=ADMIN_HEADERS)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_get_host_returns_fields(self):
        """Response includes expected fields from HostResponse schema."""
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="running", ip_address="10.0.0.50")
        resp = client.get(f"/api/v1/hosts/{hid}", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "running"
        assert data["ip_address"] == "10.0.0.50"

    def test_get_host_requires_operator(self):
        hid = _create_host()
        resp = client.get(f"/api/v1/hosts/{hid}", headers=USER_HEADERS)
        assert resp.status_code == 403


# ===================================================================
# GET /hosts/summary
# ===================================================================


class TestHostSummary:
    def test_summary_returns_list(self):
        with patch("app.services.placement.get_allocatable", return_value=(0, 0)):
            resp = client.get("/api/v1/hosts/summary", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_summary_includes_region_data(self):
        """Each entry has expected region summary fields."""
        prov_id = _create_provider()
        _create_host(provider_id=prov_id, state="active")

        with patch("app.services.placement.get_allocatable", return_value=(16, 32768)):
            resp = client.get("/api/v1/hosts/summary", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        if data:
            entry = data[0]
            assert "total_hosts" in entry
            assert "active_hosts" in entry
            assert "total_vcpus" in entry
            assert "total_ram_mb" in entry

    def test_summary_requires_operator(self):
        resp = client.get("/api/v1/hosts/summary", headers=USER_HEADERS)
        assert resp.status_code == 403


# ===================================================================
# PATCH /hosts/{id}
# ===================================================================


class TestUpdateHost:
    def test_update_auto_extend_fields(self):
        hid = _create_host()
        resp = client.patch(
            f"/api/v1/hosts/{hid}",
            json={"auto_extend_enabled": True, "auto_extend_threshold_pct": 85},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

    def test_update_disallowed_field(self):
        hid = _create_host()
        resp = client.patch(
            f"/api/v1/hosts/{hid}",
            json={"ip_address": "evil"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "Cannot update field" in resp.json()["detail"]

    def test_update_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.patch(
            f"/api/v1/hosts/{fake_id}",
            json={"auto_extend_enabled": True},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_update_requires_admin(self):
        hid = _create_host()
        resp = client.patch(
            f"/api/v1/hosts/{hid}",
            json={"auto_extend_enabled": True},
            headers=USER_HEADERS,
        )
        assert resp.status_code == 403


# ===================================================================
# GET /hosts/{id}/ssh-key  (lines 950-997)
# ===================================================================


class TestGetSSHKey:
    def test_ssh_key_success_ec2(self):
        prov_id = _create_provider(provider_type="ec2")
        hid = _create_host(
            provider_id=prov_id,
            private_key="-----BEGIN RSA KEY-----\nfake\n-----END RSA KEY-----",
            ip_address="10.0.0.50",
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ssh-rsa AAAA... user@host"
            )
            resp = client.get(f"/api/v1/hosts/{hid}/ssh-key", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["private_key"].startswith("-----BEGIN RSA KEY-----")
        assert "ssh_command" in data
        assert "ec2-user" in data["ssh_command"]
        assert "-p " not in data["ssh_command"]  # port 22 omits flag
        assert "public_key" in data
        assert data["ssh_script_command"].startswith("scripts/host-ssh.sh")

    def test_ssh_key_ocpvirt_provider(self):
        prov_id = _create_provider(provider_type="ocpvirt")
        hid = _create_host(
            provider_id=prov_id,
            private_key="fake-key",
            ip_address="10.0.0.60",
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            resp = client.get(f"/api/v1/hosts/{hid}/ssh-key", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "cloud-user" in data["ssh_command"]
        assert "-p 22000" in data["ssh_command"]
        assert "public_key" not in data  # keygen failed

    def test_ssh_key_no_ip_omits_command(self):
        hid = _create_host(private_key="fake-key", ip_address="")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            resp = client.get(f"/api/v1/hosts/{hid}/ssh-key", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["ssh_command"] is None

    def test_ssh_key_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.get(f"/api/v1/hosts/{fake_id}/ssh-key", headers=ADMIN_HEADERS)
        assert resp.status_code == 404

    def test_ssh_key_no_key_stored(self):
        hid = _create_host(private_key="")
        resp = client.get(f"/api/v1/hosts/{hid}/ssh-key", headers=ADMIN_HEADERS)
        assert resp.status_code == 404
        assert "No SSH key" in resp.json()["detail"]

    def test_ssh_key_requires_admin(self):
        hid = _create_host(private_key="fake-key")
        resp = client.get(f"/api/v1/hosts/{hid}/ssh-key", headers=USER_HEADERS)
        assert resp.status_code == 403


# ===================================================================
# GET /hosts/{id}/ssh-key/download  (lines 1000-1020)
# ===================================================================


class TestDownloadSSHKey:
    def test_download_success(self):
        hid = _create_host(private_key="-----BEGIN KEY-----\nfake\n-----END KEY-----")
        resp = client.get(
            f"/api/v1/hosts/{hid}/ssh-key/download", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain; charset=utf-8"
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "-----BEGIN KEY-----" in resp.text

    def test_download_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.get(
            f"/api/v1/hosts/{fake_id}/ssh-key/download", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 404

    def test_download_no_key(self):
        hid = _create_host(private_key="")
        resp = client.get(
            f"/api/v1/hosts/{hid}/ssh-key/download", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 404
        assert "No SSH key" in resp.json()["detail"]


# ===================================================================
# POST /hosts/{id}/poweroff  (lines 1023-1092)
# ===================================================================


class TestPoweroffHost:
    def test_poweroff_success(self):
        prov_id = _create_provider()
        hid = _create_host(
            provider_id=prov_id,
            state="active",
            agent_status="connected",
            ip_address="10.0.0.1",
        )
        # Need a provider relationship loaded
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-abc123"
        db.commit()
        db.close()

        with patch("app.core.redis.enqueue_job") as mock_enqueue, patch(
            "app.services.placement.sync_host_capacity"
        ):
            resp = client.post(f"/api/v1/hosts/{hid}/poweroff", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        mock_enqueue.assert_called_once()

        # Verify state changed in DB
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        assert h.state == "stopped"
        assert h.agent_status == "disconnected"
        assert h.ip_address is None
        db.close()

    def test_poweroff_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(f"/api/v1/hosts/{fake_id}/poweroff", headers=ADMIN_HEADERS)
        assert resp.status_code == 404

    def test_poweroff_no_instance_id(self):
        hid = _create_host(state="active")
        resp = client.post(f"/api/v1/hosts/{hid}/poweroff", headers=ADMIN_HEADERS)
        assert resp.status_code == 400
        assert "No instance ID" in resp.json()["detail"]

    def test_poweroff_running_projects_blocked(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="active")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-test"
        p = Project(name="Active Proj", owner_id=ADMIN_ID, state="active", host_id=hid)
        db.add(p)
        db.commit()
        db.close()

        resp = client.post(f"/api/v1/hosts/{hid}/poweroff", headers=ADMIN_HEADERS)
        assert resp.status_code == 409
        assert "running project" in resp.json()["detail"]

    def test_poweroff_no_provider(self):
        """Host with instance_id but no provider should fail."""
        hid = _create_host(state="active")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-noprov"
        h.provider_id = None
        db.commit()
        db.close()

        resp = client.post(f"/api/v1/hosts/{hid}/poweroff", headers=ADMIN_HEADERS)
        assert resp.status_code == 400
        assert "No provider" in resp.json()["detail"]


# ===================================================================
# POST /hosts/{id}/poweron  (lines 1099-1177)
# ===================================================================


class TestPoweronHost:
    def test_poweron_success(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="stopped")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-on123"
        db.commit()
        db.close()

        with patch("app.core.redis.enqueue_job") as mock_enqueue:
            resp = client.post(f"/api/v1/hosts/{hid}/poweron", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "starting"
        mock_enqueue.assert_called_once()

    def test_poweron_with_resize(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="stopped")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-resize"
        h.instance_type = "r6i.4xlarge"
        db.commit()
        db.close()

        mock_drv = MagicMock()
        mock_drv.resize_host.return_value = {
            "instance_type": "r6i.8xlarge",
            "total_vcpus": 32,
            "total_ram_mb": 262144,
            "max_eips": 14,
        }
        with patch("app.core.redis.enqueue_job"), patch(
            "app.services.providers.get_provider_driver", return_value=mock_drv
        ):
            resp = client.post(
                f"/api/v1/hosts/{hid}/poweron",
                json={"instance_type": "r6i.8xlarge"},
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "starting"

    def test_poweron_resize_not_stopped(self):
        """Resize during poweron requires host to be stopped."""
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="active")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-notstop"
        h.instance_type = "r6i.4xlarge"
        db.commit()
        db.close()

        resp = client.post(
            f"/api/v1/hosts/{hid}/poweron",
            json={"instance_type": "r6i.8xlarge"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert "stopped to resize" in resp.json()["detail"]

    def test_poweron_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(f"/api/v1/hosts/{fake_id}/poweron", headers=ADMIN_HEADERS)
        assert resp.status_code == 404

    def test_poweron_no_instance_id(self):
        hid = _create_host(state="stopped")
        resp = client.post(f"/api/v1/hosts/{hid}/poweron", headers=ADMIN_HEADERS)
        assert resp.status_code == 400
        assert "No instance ID" in resp.json()["detail"]

    def test_poweron_no_provider(self):
        hid = _create_host(state="stopped")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-noprov2"
        h.provider_id = None
        db.commit()
        db.close()

        resp = client.post(f"/api/v1/hosts/{hid}/poweron", headers=ADMIN_HEADERS)
        assert resp.status_code == 400
        assert "No provider" in resp.json()["detail"]


# ===================================================================
# POST /hosts/{id}/resize  (lines 1312-1360)
# ===================================================================


class TestResizeHost:
    def test_resize_success(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="stopped")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-resz"
        h.instance_type = "r6i.4xlarge"
        db.commit()
        db.close()

        with patch("app.api.hosts.resize_instance") as mock_resize:
            mock_resize.return_value = {
                "instance_type": "r6i.8xlarge",
                "total_vcpus": 32,
                "total_ram_mb": 262144,
                "max_eips": 14,
            }
            resp = client.post(
                f"/api/v1/hosts/{hid}/resize",
                json={"instance_type": "r6i.8xlarge"},
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "resized"
        assert data["new_instance_type"] == "r6i.8xlarge"
        assert data["old_instance_type"] == "r6i.4xlarge"

    def test_resize_not_stopped(self):
        hid = _create_host(state="active")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-rz2"
        h.instance_type = "r6i.4xlarge"
        db.commit()
        db.close()

        resp = client.post(
            f"/api/v1/hosts/{hid}/resize",
            json={"instance_type": "r6i.8xlarge"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert "stopped" in resp.json()["detail"]

    def test_resize_no_instance_type(self):
        hid = _create_host(state="stopped")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-rz3"
        db.commit()
        db.close()

        resp = client.post(
            f"/api/v1/hosts/{hid}/resize",
            json={"instance_type": ""},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "instance_type is required" in resp.json()["detail"]

    def test_resize_same_type(self):
        hid = _create_host(state="stopped")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-rz4"
        h.instance_type = "r6i.4xlarge"
        db.commit()
        db.close()

        resp = client.post(
            f"/api/v1/hosts/{hid}/resize",
            json={"instance_type": "r6i.4xlarge"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "Already that instance type" in resp.json()["detail"]

    def test_resize_no_instance_id(self):
        hid = _create_host(state="stopped")
        resp = client.post(
            f"/api/v1/hosts/{hid}/resize",
            json={"instance_type": "r6i.8xlarge"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "No EC2 instance" in resp.json()["detail"]

    def test_resize_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/hosts/{fake_id}/resize",
            json={"instance_type": "r6i.8xlarge"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_resize_exception(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="stopped")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-rzfail"
        h.instance_type = "r6i.4xlarge"
        db.commit()
        db.close()

        with patch("app.api.hosts.resize_instance", side_effect=RuntimeError("boom")):
            resp = client.post(
                f"/api/v1/hosts/{hid}/resize",
                json={"instance_type": "r6i.8xlarge"},
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 500
        assert "Failed to resize" in resp.json()["detail"]


# ===================================================================
# POST /hosts/{id}/extend-storage  (lines 1450-1480)
# ===================================================================


class TestExtendStorage:
    def test_extend_success_with_provider(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="active")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-ext1"
        db.commit()
        db.close()

        mock_drv = MagicMock()
        mock_drv.extend_host_storage.return_value = {
            "status": "extended",
            "new_size_gb": 600,
        }
        with patch("app.services.providers.get_provider_driver", return_value=mock_drv):
            resp = client.post(
                f"/api/v1/hosts/{hid}/extend-storage",
                json={"increment_gb": 100},
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "extended"

    def test_extend_no_provider_fallback(self):
        """Host without provider uses legacy extend_host_ebs."""
        hid = _create_host(state="active")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-ext2"
        h.provider_id = None
        db.commit()
        db.close()

        with patch(
            "app.services.storage_extend.extend_host_ebs",
            return_value={"status": "extended", "new_size_gb": 600},
        ):
            resp = client.post(
                f"/api/v1/hosts/{hid}/extend-storage",
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200

    def test_extend_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/hosts/{fake_id}/extend-storage",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_extend_no_instance(self):
        hid = _create_host(state="active")
        resp = client.post(
            f"/api/v1/hosts/{hid}/extend-storage",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "No instance" in resp.json()["detail"]

    def test_extend_value_error(self):
        prov_id = _create_provider()
        hid = _create_host(provider_id=prov_id, state="active")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.instance_id = "i-ext-fail"
        db.commit()
        db.close()

        mock_drv = MagicMock()
        mock_drv.extend_host_storage.side_effect = ValueError("At max size already")
        with patch("app.services.providers.get_provider_driver", return_value=mock_drv):
            resp = client.post(
                f"/api/v1/hosts/{hid}/extend-storage",
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 400
        assert "At max size" in resp.json()["detail"]


# ===================================================================
# POST /hosts/{id}/wipe  (lines 1738-1835)
# ===================================================================


class TestWipeHost:
    def test_wipe_no_projects(self):
        hid = _create_host(state="active", agent_status="connected", agent_token="tok")

        with patch(
            "app.services.troshkad_client.start_job", return_value="job-1"
        ), patch(
            "app.services.troshkad_client.wait_for_job",
            return_value={
                "status": "completed",
                "result": {
                    "orphan_dirs": [],
                    "orphan_domains": [],
                    "orphan_bridges": [],
                    "orphan_namespaces": [],
                },
            },
        ), patch(
            "app.services.placement.sync_host_capacity"
        ):
            resp = client.post(f"/api/v1/hosts/{hid}/wipe", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["projects_reset"] == 0
        assert data["projects_destroyed"] == 0

    def test_wipe_destroys_active_projects(self):
        prov_id = _create_provider()
        hid = _create_host(
            provider_id=prov_id,
            state="active",
            agent_status="connected",
            agent_token="tok",
        )
        db = TestSession()
        p = Project(
            name="Wipe Target",
            owner_id=ADMIN_ID,
            state="active",
            host_id=hid,
        )
        db.add(p)
        db.commit()
        db.close()

        with patch(
            "app.services.deploy_service.destroy_project_sync"
        ) as mock_destroy, patch(
            "app.services.troshkad_client.start_job", return_value="job-1"
        ), patch(
            "app.services.troshkad_client.wait_for_job",
            return_value={
                "status": "completed",
                "result": {
                    "orphan_dirs": [],
                    "orphan_domains": [],
                    "orphan_bridges": [],
                    "orphan_namespaces": [],
                },
            },
        ), patch(
            "app.services.placement.sync_host_capacity"
        ):
            resp = client.post(f"/api/v1/hosts/{hid}/wipe", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["projects_destroyed"] >= 1
        mock_destroy.assert_called()

    def test_wipe_resets_error_projects_with_networks(self):
        """Projects in error state with vni_map trigger network teardown."""
        prov_id = _create_provider()
        hid = _create_host(
            provider_id=prov_id,
            state="active",
            agent_status="connected",
            agent_token="tok",
        )
        db = TestSession()
        p = Project(
            name="Error Project",
            owner_id=ADMIN_ID,
            state="error",
            host_id=hid,
            vni_map={"net1": 100},
        )
        db.add(p)
        db.commit()
        pid = p.id
        db.close()

        with patch(
            "app.services.deploy_service._teardown_networks_via_troshkad"
        ) as mock_teardown, patch(
            "app.services.troshkad_client.start_job", return_value="job-1"
        ), patch(
            "app.services.troshkad_client.wait_for_job",
            return_value={
                "status": "completed",
                "result": {
                    "orphan_dirs": [],
                    "orphan_domains": [],
                    "orphan_bridges": [],
                    "orphan_namespaces": [],
                },
            },
        ), patch(
            "app.services.placement.sync_host_capacity"
        ):
            resp = client.post(f"/api/v1/hosts/{hid}/wipe", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["projects_reset"] >= 1
        mock_teardown.assert_called()

        # Project should be reset to draft
        db = TestSession()
        p = db.query(Project).filter_by(id=pid).first()
        assert p.state == "draft"
        db.close()

    def test_wipe_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = client.post(f"/api/v1/hosts/{fake_id}/wipe", headers=ADMIN_HEADERS)
        assert resp.status_code == 404

    def test_wipe_troshkad_error(self):
        """Wipe continues even if troshkad GC fails."""
        hid = _create_host(state="active", agent_status="connected", agent_token="tok")
        from app.services.troshkad_client import TroshkadError

        with patch(
            "app.services.troshkad_client.start_job",
            side_effect=TroshkadError("connection refused"),
        ), patch("app.services.placement.sync_host_capacity"):
            resp = client.post(f"/api/v1/hosts/{hid}/wipe", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data["cleanup"]


# ===================================================================
# POST /hosts/{id}/update-agent (body lines 1871-1905)
# ===================================================================


class TestUpdateAgentBody:
    def test_update_agent_success(self):
        hid = _create_host(agent_status="connected", agent_token="tok-up")

        with patch("os.path.exists", return_value=True), patch(
            "builtins.open",
            return_value=MagicMock(
                __enter__=lambda s: MagicMock(read=lambda: b'VERSION = "dev"\n# rest'),
                __exit__=MagicMock(return_value=False),
            ),
        ), patch("app.core.redis.enqueue_job") as mock_enqueue:
            resp = client.post(
                f"/api/v1/hosts/{hid}/update-agent",
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updating"
        assert "version" in data
        mock_enqueue.assert_called_once()

    def test_update_agent_up_to_date(self):
        """Agent with matching version skips update (without force)."""
        import hashlib

        script = b'VERSION = "dev"\n# rest'
        version = hashlib.sha256(script).hexdigest()[:12]

        hid = _create_host(agent_status="connected", agent_token="tok-upd")
        db = TestSession()
        h = db.query(Host).filter_by(id=hid).first()
        h.agent_version = version
        db.commit()
        db.close()

        with patch("os.path.exists", return_value=True), patch(
            "builtins.open",
            return_value=MagicMock(
                __enter__=lambda s: MagicMock(read=lambda: script),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            resp = client.post(
                f"/api/v1/hosts/{hid}/update-agent",
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "up_to_date"

    def test_update_agent_troshkad_missing(self):
        hid = _create_host(agent_status="connected", agent_token="tok-miss")

        with patch("os.path.exists", return_value=False):
            resp = client.post(
                f"/api/v1/hosts/{hid}/update-agent",
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == 500
        assert "troshkad.py not found" in resp.json()["detail"]


# ===================================================================
# _provision_and_install_bg  (lines 748-821)
# ===================================================================


class TestProvisionAndInstallBg:
    @patch("app.core.database.SessionLocal")
    @patch("app.services.providers.get_provider_driver")
    def test_happy_path(self, mock_get_drv, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        host = MagicMock()
        host.id = "h-1234567890"
        host.instance_id = None
        host.ip_address = None
        host.private_key = "key"
        host.console_domain = ""
        provider = MagicMock()
        provider.type = "ec2"
        provider.get_credentials.return_value = {}
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            host,
            provider,
        ]

        mock_drv = MagicMock()
        mock_drv.provision_host.return_value = {
            "instance_id": "i-new",
            "instance_type": "r6i.4xlarge",
            "total_vcpus": 16,
            "total_ram_mb": 131072,
            "public_ip": "1.2.3.4",
            "private_ip": "10.0.0.5",
            "key_pair_name": "kp",
            "private_key": "newkey",
            "storage_size_gb": 500,
            "max_eips": 8,
        }
        mock_get_drv.return_value = mock_drv

        from app.api.hosts import _provision_and_install_bg

        with patch("app.api.hosts._do_ssh_wait_and_install"):
            _provision_and_install_bg(
                host_id="h-1234567890",
                _provider_id="prov-1",
                _image_id="ami-123",
                _instance_type="r6i.4xlarge",
                _vpc_id="vpc-1",
                _subnet_id="sub-1",
                _sg_id="sg-1",
                _pool_id=None,
                provider_type="ec2",
                provider_console_domain="console.example.com",
                _disk_gb=500,
                region="us-east-1",
                nfs_kwargs=None,
            )

        assert host.instance_id == "i-new"
        assert host.state == "active"
        assert host.ip_address == "1.2.3.4"

    @patch("app.core.database.SessionLocal")
    @patch("app.services.providers.get_provider_driver")
    def test_kubevirt_skips_ssh(self, mock_get_drv, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        host = MagicMock()
        host.id = "h-kv12345678"
        provider = MagicMock()
        provider.type = "kubevirt"
        provider.get_credentials.return_value = {"token": "kube-tok"}
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            host,
            provider,
        ]

        mock_drv = MagicMock()
        mock_drv.provision_host.return_value = {
            "instance_id": "kv-cluster",
            "instance_type": "kubevirt",
            "total_vcpus": 64,
            "total_ram_mb": 524288,
            "public_ip": "",
            "private_ip": None,
        }
        mock_get_drv.return_value = mock_drv

        from app.api.hosts import _provision_and_install_bg

        _provision_and_install_bg(
            host_id="h-kv12345678",
            _provider_id="prov-kv",
            _image_id="",
            _instance_type="kubevirt",
            _vpc_id="",
            _subnet_id="",
            _sg_id="",
            _pool_id=None,
            provider_type="kubevirt",
            provider_console_domain="",
        )

        assert host.host_type == "kubevirt-cluster"
        assert host.agent_status == "connected"

    @patch("app.core.database.SessionLocal")
    @patch("app.services.providers.get_provider_driver")
    def test_provision_failure(self, mock_get_drv, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        host = MagicMock()
        host.id = "h-fail12345"
        provider = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            host,
            provider,
        ]

        mock_drv = MagicMock()
        mock_drv.provision_host.side_effect = RuntimeError("cloud error")
        mock_get_drv.return_value = mock_drv

        from app.api.hosts import _provision_and_install_bg

        _provision_and_install_bg(
            host_id="h-fail12345",
            _provider_id="prov-x",
            _image_id="ami-x",
            _instance_type="r6i.4xlarge",
            _vpc_id="vpc-x",
            _subnet_id="sub-x",
            _sg_id="sg-x",
            _pool_id=None,
            provider_type="ec2",
            provider_console_domain="",
        )

        assert host.state == "error"
        assert host.agent_status == "provision_failed"


# ===================================================================
# _install_bg  (lines 866-947)
# ===================================================================


class TestInstallBg:
    @patch("app.core.database.SessionLocal")
    def test_install_bg_success(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        host = MagicMock()
        host.id = "h-inst12345"
        host.provider_id = None
        provider_mock = None
        mock_session.query.return_value.filter_by.return_value.first.return_value = host
        mock_session.get.return_value = provider_mock

        from app.api.hosts import _install_bg

        with patch(
            "app.services.agent_deployer.wait_for_ssh", return_value=True
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user"
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_port", return_value=22
        ), patch(
            "app.services.agent_deployer.get_provider_data_disk",
            return_value="/dev/sdf",
        ), patch(
            "app.services.agent_deployer.deploy_agent",
            return_value={
                "success": True,
                "troshkad_credentials": {"token": "new-tok", "fingerprint": "fp1"},
            },
        ), patch(
            "app.api.hosts._build_pool_install_kwargs", return_value={}
        ), patch(
            "app.api.hosts._verify_and_update_agent_version"
        ):
            _install_bg("h-inst12345", "10.0.0.1", "fake-key")

        assert host.agent_status == "connected"
        assert host.agent_token == "new-tok"
        assert host.agent_cert_fingerprint == "fp1"

    @patch("app.core.database.SessionLocal")
    def test_install_bg_ssh_timeout(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        host = MagicMock()
        host.id = "h-notimeout"
        host.provider_id = None
        mock_session.query.return_value.filter_by.return_value.first.return_value = host
        mock_session.get.return_value = None

        from app.api.hosts import _install_bg

        with patch(
            "app.services.agent_deployer.wait_for_ssh", return_value=False
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user"
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_port", return_value=22
        ), patch(
            "app.services.agent_deployer.get_provider_data_disk",
            return_value="/dev/sdf",
        ):
            _install_bg("h-notimeout", "10.0.0.1", "fake-key")

        assert host.agent_status == "disconnected"

    @patch("app.core.database.SessionLocal")
    def test_install_bg_exception(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        host = MagicMock()
        host.id = "h-excep12345"
        host.provider_id = None
        # First call in main try block, second call in except block
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            host,
            host,
        ]
        mock_session.get.return_value = None

        from app.api.hosts import _install_bg

        with patch(
            "app.services.agent_deployer.wait_for_ssh", return_value=True
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_user", return_value="ec2-user"
        ), patch(
            "app.services.agent_deployer.get_provider_ssh_port", return_value=22
        ), patch(
            "app.services.agent_deployer.get_provider_data_disk",
            return_value="/dev/sdf",
        ), patch(
            "app.api.hosts._build_pool_install_kwargs",
            side_effect=RuntimeError("pool error"),
        ):
            _install_bg("h-excep12345", "10.0.0.1", "fake-key")

        assert host.agent_status == "install_failed"


# ===================================================================
# _wait_terminated_bg  (lines 1668-1699)
# ===================================================================


class TestWaitTerminatedBg:
    @patch("time.sleep")
    @patch("app.core.database.SessionLocal")
    def test_terminated_state(self, mock_session_cls, mock_sleep):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        provider = MagicMock()
        mock_session.get.return_value = provider

        mock_drv = MagicMock()
        mock_drv.get_host_status.return_value = {"state": "terminated"}

        host = MagicMock()
        host.id = "h-term12345"
        mock_session.query.return_value.filter_by.return_value.first.return_value = host

        from app.api.hosts import _wait_terminated_bg

        with patch(
            "app.services.providers.get_provider_driver", return_value=mock_drv
        ), patch("app.api.hosts._finalize_termination") as mock_final:
            _wait_terminated_bg("h-term12345", "i-term", "prov-1")

        mock_final.assert_called_once()

    @patch("time.sleep")
    @patch("app.core.database.SessionLocal")
    def test_shutting_down_then_terminated(self, mock_session_cls, mock_sleep):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        provider = MagicMock()
        mock_session.get.return_value = provider

        mock_drv = MagicMock()
        mock_drv.get_host_status.side_effect = [
            {"state": "shutting-down"},
            {"state": "terminated"},
        ]

        host = MagicMock()
        host.id = "h-sdterm1234"
        mock_session.query.return_value.filter_by.return_value.first.return_value = host

        from app.api.hosts import _wait_terminated_bg

        with patch(
            "app.services.providers.get_provider_driver", return_value=mock_drv
        ), patch("app.api.hosts._finalize_termination") as mock_final:
            _wait_terminated_bg("h-sdterm1234", "i-sd", "prov-1")

        # First iteration sets shutting_down
        assert host.state == "shutting_down"
        # Second iteration finalizes
        mock_final.assert_called_once()

    @patch("time.sleep")
    @patch("app.core.database.SessionLocal")
    def test_no_provider(self, mock_session_cls, mock_sleep):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.return_value = None

        host = MagicMock()
        host.id = "h-noprov1234"
        mock_session.query.return_value.filter_by.return_value.first.return_value = host

        from app.api.hosts import _wait_terminated_bg

        with patch("app.api.hosts._finalize_termination") as mock_final:
            _wait_terminated_bg("h-noprov1234", "i-noprov", "")

        # None status → finalize immediately
        mock_final.assert_called_once()
