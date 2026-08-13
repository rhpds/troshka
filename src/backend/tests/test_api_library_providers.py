"""Tests for /api/v1/library and /api/v1/providers endpoints.

Targets uncovered new-code lines in app/api/library.py and app/api/providers.py
for SonarQube coverage improvement.
"""

import uuid

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.library import Library, LibraryItem
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def _ensure_dev_user():
    """Ensure the dev-mode user exists and return its ID."""
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


def _ensure_user_and_library():
    """Ensure dev user and personal library exist, return (user_id, lib_id)."""
    user_id = _ensure_dev_user()
    db = TestSession()
    lib = db.query(Library).filter_by(owner_id=user_id, type="personal").first()
    if not lib:
        lib = Library(
            id=str(uuid.uuid4()),
            type="personal",
            owner_id=user_id,
        )
        db.add(lib)
        db.commit()
    lib_id = lib.id
    db.close()
    return user_id, lib_id


def _create_library_item(lib_id, name="test-image", **kwargs):
    """Create a test LibraryItem in the DB and return its ID."""
    db = TestSession()
    defaults = {
        "id": str(uuid.uuid4()),
        "library_id": lib_id,
        "name": name,
        "type": "image",
        "format": "qcow2",
        "size_bytes": 1024,
        "state": "ready",
    }
    defaults.update(kwargs)
    item = LibraryItem(**defaults)
    db.add(item)
    db.commit()
    item_id = item.id
    db.close()
    return item_id


def _create_provider(name="test-provider", provider_type="ec2", **kwargs):
    """Create a test Provider in the DB and return its ID."""
    db = TestSession()
    p = Provider(
        id=str(uuid.uuid4()),
        name=name,
        type=provider_type,
        state="active",
        created_by="local-dev@troshka",
        **kwargs,
    )
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    return pid


# ===========================================================================
# Library API tests — GET /api/v1/library/
# ===========================================================================


def test_list_library_items_empty():
    """GET /library/ returns 200 and a list even when library is empty."""
    resp = client.get("/api/v1/library/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_library_items_with_items():
    """GET /library/ returns items belonging to the user's library."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"list-test-{uuid.uuid4().hex[:8]}")
    resp = client.get("/api/v1/library/")
    assert resp.status_code == 200
    data = resp.json()
    ids = [i["id"] for i in data]
    assert item_id in ids


def test_list_library_items_filter_by_type():
    """GET /library/?type=iso filters by item type."""
    _user_id, lib_id = _ensure_user_and_library()
    _create_library_item(
        lib_id,
        name=f"iso-item-{uuid.uuid4().hex[:8]}",
        type="iso",
        format="iso",
    )
    resp = client.get("/api/v1/library/", params={"type": "iso"})
    assert resp.status_code == 200
    data = resp.json()
    for item in data:
        assert item["type"] == "iso"


def test_list_library_items_filter_by_query():
    """GET /library/?q=searchterm filters by name substring."""
    _user_id, lib_id = _ensure_user_and_library()
    unique = uuid.uuid4().hex[:8]
    _create_library_item(lib_id, name=f"unique-needle-{unique}")
    resp = client.get("/api/v1/library/", params={"q": unique})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert any(unique in i["name"] for i in data)


# ===========================================================================
# Library API tests — GET /api/v1/library/{item_id}
# ===========================================================================


def test_get_library_item_success():
    """GET /library/{item_id} returns the item with all expected fields."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"get-test-{uuid.uuid4().hex[:8]}")
    resp = client.get(f"/api/v1/library/{item_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == item_id
    assert "name" in data
    assert "type" in data
    assert "format" in data
    assert "state" in data
    assert "size_bytes" in data
    assert "created_at" in data


def test_get_library_item_not_found():
    """GET /library/{item_id} returns 404 for nonexistent item."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/library/{fake_id}")
    assert resp.status_code == 404


# ===========================================================================
# Library API tests — PATCH /api/v1/library/{item_id}
# ===========================================================================


def test_update_library_item_name():
    """PATCH /library/{item_id} updates the item name."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"patch-test-{uuid.uuid4().hex[:8]}")
    new_name = f"updated-{uuid.uuid4().hex[:8]}"
    resp = client.patch(f"/api/v1/library/{item_id}", json={"name": new_name})
    assert resp.status_code == 200
    assert resp.json()["name"] == new_name


def test_update_library_item_description():
    """PATCH /library/{item_id} updates the description."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"patch-desc-{uuid.uuid4().hex[:8]}")
    resp = client.patch(
        f"/api/v1/library/{item_id}", json={"description": "new description"}
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "new description"


def test_update_library_item_not_found():
    """PATCH /library/{item_id} returns 404 for nonexistent item."""
    fake_id = str(uuid.uuid4())
    resp = client.patch(f"/api/v1/library/{fake_id}", json={"name": "x"})
    assert resp.status_code == 404


def test_update_library_item_tags():
    """PATCH /library/{item_id} updates tags dict."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"patch-tags-{uuid.uuid4().hex[:8]}")
    resp = client.patch(
        f"/api/v1/library/{item_id}", json={"tags": {"custom_tag": True}}
    )
    assert resp.status_code == 200


def test_update_library_item_ocp_default_image_tag():
    """PATCH /library/{item_id} with ocp_default_image clears other defaults."""
    _user_id, lib_id = _ensure_user_and_library()
    # Create two items, both with ocp_default_image
    item1_id = _create_library_item(
        lib_id,
        name=f"ocp-default-1-{uuid.uuid4().hex[:8]}",
        tags={"ocp_default_image": True},
    )
    item2_id = _create_library_item(
        lib_id, name=f"ocp-default-2-{uuid.uuid4().hex[:8]}"
    )
    # Set item2 as ocp_default_image — should clear item1
    resp = client.patch(
        f"/api/v1/library/{item2_id}",
        json={"tags": {"ocp_default_image": True}},
    )
    assert resp.status_code == 200

    # Verify item1 no longer has the tag
    db = TestSession()
    item1 = db.query(LibraryItem).filter_by(id=item1_id).first()
    tags = item1.tags if isinstance(item1.tags, dict) else {}
    assert not tags.get("ocp_default_image"), "ocp_default_image should be cleared"
    db.close()


def test_update_library_item_ocp_default_iso_tag():
    """PATCH /library/{item_id} with ocp_default_iso clears other defaults."""
    _user_id, lib_id = _ensure_user_and_library()
    item1_id = _create_library_item(
        lib_id,
        name=f"ocp-iso-1-{uuid.uuid4().hex[:8]}",
        tags={"ocp_default_iso": True},
    )
    item2_id = _create_library_item(lib_id, name=f"ocp-iso-2-{uuid.uuid4().hex[:8]}")
    resp = client.patch(
        f"/api/v1/library/{item2_id}",
        json={"tags": {"ocp_default_iso": True}},
    )
    assert resp.status_code == 200

    db = TestSession()
    item1 = db.query(LibraryItem).filter_by(id=item1_id).first()
    tags = item1.tags if isinstance(item1.tags, dict) else {}
    assert not tags.get("ocp_default_iso"), "ocp_default_iso should be cleared"
    db.close()


def test_update_library_item_central_readonly():
    """PATCH /library/{item_id} returns 403 for central (read-only) items."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"central-item-{uuid.uuid4().hex[:8]}",
        source="central",
    )
    resp = client.patch(f"/api/v1/library/{item_id}", json={"name": "nope"})
    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"]


# ===========================================================================
# Library API tests — POST /api/v1/library/
# ===========================================================================


def test_create_library_item():
    """POST /library/ creates a new item and returns 201."""
    unique = uuid.uuid4().hex[:8]
    resp = client.post(
        "/api/v1/library/",
        json={
            "name": f"new-item-{unique}",
            "type": "image",
            "format": "qcow2",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert data["state"] == "pending"


def test_create_library_item_duplicate_name():
    """POST /library/ returns 409 when item name already exists."""
    name = f"dup-item-{uuid.uuid4().hex[:8]}"
    resp1 = client.post(
        "/api/v1/library/",
        json={"name": name, "type": "image", "format": "qcow2"},
    )
    assert resp1.status_code == 201

    resp2 = client.post(
        "/api/v1/library/",
        json={"name": name, "type": "image", "format": "qcow2"},
    )
    assert resp2.status_code == 409
    assert "already have" in resp2.json()["detail"]


def test_create_library_item_with_optional_fields():
    """POST /library/ accepts optional description, os_variant, tags."""
    unique = uuid.uuid4().hex[:8]
    resp = client.post(
        "/api/v1/library/",
        json={
            "name": f"full-item-{unique}",
            "description": "a test image",
            "type": "iso",
            "format": "iso",
            "os_variant": "rhel9",
            "tags": ["test"],
        },
    )
    assert resp.status_code == 201


# ===========================================================================
# Library API tests — DELETE /api/v1/library/{item_id}
# ===========================================================================


def test_delete_library_item_not_found():
    """DELETE /library/{item_id} returns 404 for nonexistent item."""
    fake_id = str(uuid.uuid4())
    resp = client.delete(f"/api/v1/library/{fake_id}")
    assert resp.status_code == 404


def test_delete_library_item_central_readonly():
    """DELETE /library/{item_id} returns 403 for central items."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"central-del-{uuid.uuid4().hex[:8]}",
        source="central",
    )
    resp = client.delete(f"/api/v1/library/{item_id}")
    assert resp.status_code == 403


# ===========================================================================
# Provider API tests — GET /api/v1/providers/
# ===========================================================================


def test_list_providers_empty():
    """GET /providers/ returns 200 and a list."""
    resp = client.get("/api/v1/providers/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_providers_with_provider():
    """GET /providers/ includes created providers."""
    name = f"prov-list-{uuid.uuid4().hex[:8]}"
    pid = _create_provider(name=name)
    resp = client.get("/api/v1/providers/")
    assert resp.status_code == 200
    data = resp.json()
    ids = [p["id"] for p in data]
    assert pid in ids
    # Verify response shape
    prov = next(p for p in data if p["id"] == pid)
    assert prov["name"] == name
    assert prov["type"] == "ec2"
    assert "state" in prov
    assert "has_credentials" in prov
    assert "host_count" in prov
    assert "created_at" in prov


# ===========================================================================
# Provider API tests — GET /api/v1/providers/{id} (via response model)
# ===========================================================================


def test_get_provider_not_found_via_patch():
    """PATCH /providers/{id} returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.patch(f"/api/v1/providers/{fake_id}", json={"name": "x"})
    assert resp.status_code == 404


def test_get_provider_not_found_via_delete():
    """DELETE /providers/{id} returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.delete(f"/api/v1/providers/{fake_id}")
    assert resp.status_code == 404


# ===========================================================================
# Provider API tests — PATCH /api/v1/providers/{id}
# ===========================================================================


def test_update_provider_name():
    """PATCH /providers/{id} updates the provider name."""
    pid = _create_provider(name=f"upd-name-{uuid.uuid4().hex[:8]}")
    new_name = f"updated-{uuid.uuid4().hex[:8]}"
    resp = client.patch(f"/api/v1/providers/{pid}", json={"name": new_name})
    assert resp.status_code == 200
    assert resp.json()["name"] == new_name


def test_update_provider_region_and_image():
    """PATCH /providers/{id} updates region and image fields."""
    pid = _create_provider(name=f"upd-region-{uuid.uuid4().hex[:8]}")
    resp = client.patch(
        f"/api/v1/providers/{pid}",
        json={"default_region": "us-west-2", "default_image": "ami-12345"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["default_region"] == "us-west-2"
    assert data["default_image"] == "ami-12345"


def test_update_provider_vpc_subnet_sg():
    """PATCH /providers/{id} updates VPC networking fields."""
    pid = _create_provider(name=f"upd-vpc-{uuid.uuid4().hex[:8]}")
    resp = client.patch(
        f"/api/v1/providers/{pid}",
        json={
            "vpc_id": "vpc-abc123",
            "subnet_id": "subnet-def456",
            "security_group_id": "sg-ghi789",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vpc_id"] == "vpc-abc123"
    assert data["subnet_id"] == "subnet-def456"
    assert data["security_group_id"] == "sg-ghi789"


def test_update_provider_state():
    """PATCH /providers/{id} updates the state field."""
    pid = _create_provider(name=f"upd-state-{uuid.uuid4().hex[:8]}")
    resp = client.patch(f"/api/v1/providers/{pid}", json={"state": "disabled"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "disabled"


# ===========================================================================
# Provider API tests — DELETE /api/v1/providers/{id}
# ===========================================================================


def test_delete_provider_success():
    """DELETE /providers/{id} removes a provider with no hosts."""
    pid = _create_provider(name=f"del-ok-{uuid.uuid4().hex[:8]}")
    resp = client.delete(f"/api/v1/providers/{pid}")
    assert resp.status_code == 204

    # Verify it's gone
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    assert p is None
    db.close()


def test_delete_provider_with_hosts_rejected():
    """DELETE /providers/{id} returns 409 when provider has hosts."""
    from app.models.host import Host

    pid = _create_provider(name=f"del-hosts-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    host = Host(
        id=str(uuid.uuid4()),
        provider_id=pid,
        instance_id="i-fake",
        instance_type="m5.xlarge",
        region="us-east-1",
        state="active",
        ip_address="1.2.3.4",
        agent_status="connected",
        total_vcpus=4,
        total_ram_mb=16384,
        storage_size_gb=100,
        max_eips=5,
    )
    db.add(host)
    db.commit()
    db.close()

    resp = client.delete(f"/api/v1/providers/{pid}")
    assert resp.status_code == 409
    assert "hosts" in resp.json()["detail"].lower()


# ===========================================================================
# Provider API tests — POST /api/v1/providers/{id}/set-image
# ===========================================================================


def test_set_image_success():
    """POST /providers/{id}/set-image sets the default image."""
    pid = _create_provider(name=f"set-img-{uuid.uuid4().hex[:8]}")
    resp = client.post(
        f"/api/v1/providers/{pid}/set-image", params={"image_id": "ami-new123"}
    )
    assert resp.status_code == 200
    assert resp.json()["image_id"] == "ami-new123"


def test_set_image_not_found():
    """POST /providers/{id}/set-image returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/providers/{fake_id}/set-image", params={"image_id": "ami-x"}
    )
    assert resp.status_code == 404


# ===========================================================================
# Provider API tests — POST /api/v1/providers/{id}/set-iso
# ===========================================================================


def test_set_iso_success():
    """POST /providers/{id}/set-iso sets the ISO PVC name."""
    pid = _create_provider(
        name=f"set-iso-{uuid.uuid4().hex[:8]}", provider_type="ocpvirt"
    )
    # Set some credentials first so get_credentials returns a dict
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials({"api_url": "https://api.test", "token": "tok"})
    db.commit()
    db.close()

    resp = client.post(
        f"/api/v1/providers/{pid}/set-iso", params={"iso_pvc": "rhel-iso-pvc"}
    )
    assert resp.status_code == 200
    assert resp.json()["iso_pvc"] == "rhel-iso-pvc"


def test_set_iso_not_found():
    """POST /providers/{id}/set-iso returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/providers/{fake_id}/set-iso", params={"iso_pvc": "x"})
    assert resp.status_code == 404


# ===========================================================================
# Provider API tests — POST /api/v1/providers/ (create with different types)
# ===========================================================================


def test_create_provider_ec2():
    """POST /providers/ creates an EC2 provider with credentials."""
    name = f"ec2-create-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "ec2",
            "default_region": "us-east-1",
            "access_key_id": "test-access-key-id",  # pragma: allowlist secret
            "secret_access_key": "test-secret-key",  # pragma: allowlist secret
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == name
    assert data["type"] == "ec2"
    assert data["has_credentials"] is True
    assert data["default_region"] == "us-east-1"


from unittest.mock import MagicMock, patch


def test_create_provider_ocpvirt():
    """POST /providers/ creates an OCP Virt provider with placeholder host."""
    name = f"ocpv-create-{uuid.uuid4().hex[:8]}"
    with patch("app.api.providers._enqueue_cluster_host_provision") as mock_enqueue:
        resp = client.post(
            "/api/v1/providers/",
            json={
                "name": name,
                "type": "ocpvirt",
                "api_url": "https://api.cluster.example.com:6443",
                "token": "sha256~fake-token",
                "namespace": "troshka",
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "ocpvirt"
    assert data["has_credentials"] is True
    # console_base_domain derived from api_url
    assert "apps.cluster.example.com" in (data.get("console_base_domain") or "")
    mock_enqueue.assert_called_once()


def test_create_provider_kubevirt():
    """POST /providers/ creates a KubeVirt provider."""
    name = f"kv-create-{uuid.uuid4().hex[:8]}"
    with patch("app.api.providers._enqueue_cluster_host_provision") as mock_enqueue:
        resp = client.post(
            "/api/v1/providers/",
            json={
                "name": name,
                "type": "kubevirt",
                "api_url": "https://api.kv-cluster.example.com:6443",
                "token": "sha256~fake-kv-token",
                "namespace": "troshka-operator",
                "cache_namespace": "troshka-cache",
                "project_prefix": "troshka-",
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "kubevirt"
    assert data["has_credentials"] is True
    mock_enqueue.assert_called_once()


def test_create_provider_gcp():
    """POST /providers/ creates a GCP provider with service account JSON."""
    name = f"gcp-create-{uuid.uuid4().hex[:8]}"
    sa_json = '{"type":"service_account","project_id":"test","private_key":"pk","client_email":"sa@test.iam"}'
    resp = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "gcp",
            "gcp_project_id": "test-project-123",
            "service_account_json": sa_json,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "gcp"
    assert data["gcp_project_id"] == "test-project-123"
    assert data["has_credentials"] is True


def test_create_provider_azure():
    """POST /providers/ creates an Azure provider with SP credentials."""
    name = f"azure-create-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "azure",
            "azure_tenant_id": "tenant-id-123",
            "azure_client_id": "client-id-456",
            "azure_client_secret": "client-secret-789",
            "azure_subscription_id": "sub-id-abc",
            "azure_location": "eastus",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "azure"
    assert data["azure_subscription_id"] == "sub-id-abc"
    assert data["azure_location"] == "eastus"
    assert data["has_credentials"] is True


def test_create_provider_gcp_missing_project_id():
    """POST /providers/ returns 400 for GCP without gcp_project_id."""
    name = f"gcp-bad-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "gcp",
            "service_account_json": '{"type":"service_account"}',
        },
    )
    assert resp.status_code == 400
    assert "gcp_project_id" in resp.json()["detail"]


def test_create_provider_gcp_invalid_json():
    """POST /providers/ returns 400 for GCP with invalid service_account_json."""
    name = f"gcp-badjson-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "gcp",
            "gcp_project_id": "test-proj",
            "service_account_json": "not-valid-json{",
        },
    )
    assert resp.status_code == 400
    assert "valid JSON" in resp.json()["detail"]


def test_create_provider_azure_missing_fields():
    """POST /providers/ returns 400 for Azure with missing credentials."""
    name = f"azure-bad-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "azure",
            "azure_tenant_id": "tenant-id",
            # missing client_id, client_secret, subscription_id
        },
    )
    assert resp.status_code == 400
    assert "tenant_id" in resp.json()["detail"]


def test_create_provider_ocpvirt_missing_api_url():
    """POST /providers/ returns 400 for OCP Virt without api_url."""
    name = f"ocpv-bad-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "ocpvirt",
            "token": "sha256~tok",
            # missing api_url
        },
    )
    assert resp.status_code == 400
    assert "api_url" in resp.json()["detail"]


def test_create_provider_duplicate_name():
    """POST /providers/ returns 409 for duplicate provider name."""
    name = f"dup-prov-{uuid.uuid4().hex[:8]}"
    resp1 = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "ec2",
            "access_key_id": "AKIA1",
            "secret_access_key": "secret1",
        },
    )
    assert resp1.status_code == 201

    resp2 = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "ec2",
            "access_key_id": "AKIA2",
            "secret_access_key": "secret2",
        },
    )
    assert resp2.status_code == 409
    assert "already exists" in resp2.json()["detail"]


def test_create_provider_unknown_type():
    """POST /providers/ returns 400 for unknown provider type."""
    name = f"unk-type-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/providers/",
        json={
            "name": name,
            "type": "digitalocean",
        },
    )
    assert resp.status_code == 400
    assert "Unknown provider type" in resp.json()["detail"]


# ===========================================================================
# Provider API tests — POST /api/v1/providers/{id}/test
# ===========================================================================


def test_test_provider_ec2_success():
    """POST /providers/{id}/test succeeds for EC2 with mocked STS."""
    pid = _create_provider(name=f"test-ec2-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/test",
    }

    with patch("boto3.client", return_value=mock_sts):
        resp = client.post(f"/api/v1/providers/{pid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["account"] == "123456789012"


def test_test_provider_not_found():
    """POST /providers/{id}/test returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/providers/{fake_id}/test")
    assert resp.status_code == 404


def test_test_provider_failure():
    """POST /providers/{id}/test returns 400 on credential failure."""
    pid = _create_provider(name=f"test-fail-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials({"access_key_id": "AKIA_BAD", "secret_access_key": "bad_secret"})
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    with patch("boto3.client", side_effect=Exception("Invalid credentials")):
        resp = client.post(f"/api/v1/providers/{pid}/test")
    assert resp.status_code == 400
    assert "failed" in resp.json()["detail"].lower()


# ===========================================================================
# Provider API tests — POST /api/v1/providers/{id}/setup-console
# ===========================================================================


def test_setup_console_ocpvirt():
    """POST /providers/{id}/setup-console sets domain for OCP Virt (no Route53)."""
    pid = _create_provider(
        name=f"console-ocpv-{uuid.uuid4().hex[:8]}", provider_type="ocpvirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials({"api_url": "https://api.test", "token": "tok"})
    db.commit()
    db.close()

    resp = client.post(
        f"/api/v1/providers/{pid}/setup-console",
        json={"base_domain": "console.example.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["base_domain"] == "console.example.com"
    assert data["zone_id"] is None
    assert data["nameservers"] == []


def test_setup_console_not_found():
    """POST /providers/{id}/setup-console returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/providers/{fake_id}/setup-console",
        json={"base_domain": "console.example.com"},
    )
    assert resp.status_code == 404


def test_setup_console_invalid_domain():
    """POST /providers/{id}/setup-console returns 400 for invalid domain."""
    pid = _create_provider(name=f"console-bad-{uuid.uuid4().hex[:8]}")
    resp = client.post(
        f"/api/v1/providers/{pid}/setup-console",
        json={"base_domain": "nodots"},
    )
    assert resp.status_code == 400
    assert "Invalid domain" in resp.json()["detail"]


# ===========================================================================
# Provider API tests — DELETE /api/v1/providers/{id}/console
# ===========================================================================


def test_delete_console_not_found():
    """DELETE /providers/{id}/console returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.delete(f"/api/v1/providers/{fake_id}/console")
    assert resp.status_code == 404


def test_delete_console_not_configured():
    """DELETE /providers/{id}/console returns 400 when no console is configured."""
    pid = _create_provider(name=f"no-console-{uuid.uuid4().hex[:8]}")
    resp = client.delete(f"/api/v1/providers/{pid}/console")
    assert resp.status_code == 400
    assert "not configured" in resp.json()["detail"].lower()


# ===========================================================================
# Provider API tests — POST /api/v1/providers/{id}/install-operator
# ===========================================================================


def test_install_operator_not_found():
    """POST /providers/{id}/install-operator returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/providers/{fake_id}/install-operator")
    assert resp.status_code == 404


def test_install_operator_non_kubevirt():
    """POST /providers/{id}/install-operator returns 400 for non-kubevirt type."""
    pid = _create_provider(name=f"op-ec2-{uuid.uuid4().hex[:8]}", provider_type="ec2")
    resp = client.post(f"/api/v1/providers/{pid}/install-operator")
    assert resp.status_code == 400
    assert "kubevirt" in resp.json()["detail"].lower()


# ===========================================================================
# Provider API tests — GET /api/v1/providers/{id}/discover-isos
# ===========================================================================


def test_discover_isos_not_found():
    """GET /providers/{id}/discover-isos returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/providers/{fake_id}/discover-isos")
    assert resp.status_code == 404


def test_discover_isos_non_ocpvirt():
    """GET /providers/{id}/discover-isos returns 400 for non-ocpvirt type."""
    pid = _create_provider(name=f"iso-ec2-{uuid.uuid4().hex[:8]}", provider_type="ec2")
    resp = client.get(f"/api/v1/providers/{pid}/discover-isos")
    assert resp.status_code == 400
    assert "OCP Virt" in resp.json()["detail"]


# ===========================================================================
# Provider API tests — GET /api/v1/providers/{id}/discover-datasources
# ===========================================================================


def test_discover_datasources_not_found():
    """GET /providers/{id}/discover-datasources returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/providers/{fake_id}/discover-datasources")
    assert resp.status_code == 404


def test_discover_datasources_non_ocpvirt():
    """GET /providers/{id}/discover-datasources returns 400 for non-ocpvirt type."""
    pid = _create_provider(name=f"ds-ec2-{uuid.uuid4().hex[:8]}", provider_type="ec2")
    resp = client.get(f"/api/v1/providers/{pid}/discover-datasources")
    assert resp.status_code == 400
    assert "OCP Virt" in resp.json()["detail"]


# ===========================================================================
# Provider API tests — POST /api/v1/providers/{id}/create-bucket
# ===========================================================================


def test_create_bucket_not_found():
    """POST /providers/{id}/create-bucket returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/providers/{fake_id}/create-bucket")
    assert resp.status_code == 404


def test_create_bucket_non_s3():
    """POST /providers/{id}/create-bucket returns 400 for non-s3 type."""
    pid = _create_provider(
        name=f"bucket-ec2-{uuid.uuid4().hex[:8]}", provider_type="ec2"
    )
    resp = client.post(f"/api/v1/providers/{pid}/create-bucket")
    assert resp.status_code == 400
    assert "S3" in resp.json()["detail"]


# ===========================================================================
# Provider API tests — GET /api/v1/providers/{id}/availability-zones
# ===========================================================================


def test_availability_zones_not_found():
    """GET /providers/{id}/availability-zones returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/providers/{fake_id}/availability-zones")
    assert resp.status_code == 404


def test_availability_zones_non_ec2():
    """GET /providers/{id}/availability-zones returns 400 for non-ec2 type."""
    pid = _create_provider(
        name=f"az-ocpv-{uuid.uuid4().hex[:8]}", provider_type="ocpvirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials({"api_url": "https://api.test", "token": "tok"})
    db.commit()
    db.close()

    resp = client.get(f"/api/v1/providers/{pid}/availability-zones")
    assert resp.status_code == 400
    assert "EC2" in resp.json()["detail"]


# ===========================================================================
# Provider API tests — GET /api/v1/providers/{id}/operator-status
# ===========================================================================


def test_operator_status_not_found():
    """GET /providers/{id}/operator-status returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/providers/{fake_id}/operator-status")
    assert resp.status_code == 404


def test_operator_status_success():
    """GET /providers/{id}/operator-status returns status for a provider."""
    pid = _create_provider(
        name=f"op-status-{uuid.uuid4().hex[:8]}", provider_type="kubevirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "api_url": "https://api.test:6443",
            "token": "tok",
            "namespace": "troshka-operator",
        }
    )
    db.commit()
    db.close()

    with patch(
        "app.services.operator_updater._get_operator_info",
        return_value=("sha256:abc123def456", False, "production"),
    ), patch(
        "app.services.operator_updater._fetch_registry_digest",
        return_value="sha256:abc123def456",
    ):
        resp = client.get(f"/api/v1/providers/{pid}/operator-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "operator_digest" in data
    assert "registry_digest" in data
    assert "up_to_date" in data
    assert "rolling_out" in data


# ===========================================================================
# Provider API tests — POST /api/v1/providers/{id}/update-operator
# ===========================================================================


def test_update_operator_not_found():
    """POST /providers/{id}/update-operator returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/providers/{fake_id}/update-operator")
    assert resp.status_code == 404


def test_update_operator_non_kubevirt():
    """POST /providers/{id}/update-operator returns 400 for non-kubevirt type."""
    pid = _create_provider(
        name=f"upd-op-ec2-{uuid.uuid4().hex[:8]}", provider_type="ec2"
    )
    resp = client.post(f"/api/v1/providers/{pid}/update-operator")
    assert resp.status_code == 400
    assert "kubevirt" in resp.json()["detail"].lower()


# ===========================================================================
# Provider API tests — POST /api/v1/providers/{id}/build-image
# ===========================================================================


def test_build_image_not_found():
    """POST /providers/{id}/build-image returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/providers/{fake_id}/build-image")
    assert resp.status_code == 404


def test_build_image_unsupported_type():
    """POST /providers/{id}/build-image returns 400 for unsupported provider type."""
    pid = _create_provider(
        name=f"build-ec2-{uuid.uuid4().hex[:8]}", provider_type="ec2"
    )
    resp = client.post(f"/api/v1/providers/{pid}/build-image")
    assert resp.status_code == 400
    assert "GCP or Azure" in resp.json()["detail"]


def test_build_image_success():
    """POST /providers/{id}/build-image starts a build for GCP provider."""
    pid = _create_provider(
        name=f"build-gcp-{uuid.uuid4().hex[:8]}", provider_type="gcp"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.gcp_project_id = "test-proj"
    p.set_credentials({"service_account_json": {"type": "service_account"}})
    db.commit()
    db.close()

    with patch(
        "app.services.image_builder_service.get_build_status",
        return_value={"status": "idle"},
    ), patch("app.core.redis.enqueue_job"):
        resp = client.post(f"/api/v1/providers/{pid}/build-image")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "started"


def test_build_image_invalid_rhel_version():
    """POST /providers/{id}/build-image returns 400 for invalid RHEL version."""
    pid = _create_provider(
        name=f"build-badver-{uuid.uuid4().hex[:8]}", provider_type="gcp"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.gcp_project_id = "test-proj"
    p.set_credentials({"service_account_json": {"type": "service_account"}})
    db.commit()
    db.close()

    with patch(
        "app.services.image_builder_service.get_build_status",
        return_value={"status": "idle"},
    ):
        resp = client.post(
            f"/api/v1/providers/{pid}/build-image",
            json={"rhel_version": "rhel-8"},
        )
    assert resp.status_code == 400
    assert "Invalid RHEL version" in resp.json()["detail"]


def test_build_image_already_in_progress():
    """POST /providers/{id}/build-image returns 409 when build is running."""
    pid = _create_provider(
        name=f"build-prog-{uuid.uuid4().hex[:8]}", provider_type="azure"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "subscription_id": "sub",
        }
    )
    db.commit()
    db.close()

    with patch(
        "app.services.image_builder_service.get_build_status",
        return_value={"status": "building"},
    ):
        resp = client.post(f"/api/v1/providers/{pid}/build-image")
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"]


# ===========================================================================
# Provider API tests — GET /api/v1/providers/{id}/build-image/status
# ===========================================================================


def test_build_image_status():
    """GET /providers/{id}/build-image/status returns build status."""
    pid = _create_provider(
        name=f"build-stat-{uuid.uuid4().hex[:8]}", provider_type="gcp"
    )
    with patch(
        "app.services.image_builder_service.get_build_status",
        return_value={"status": "idle", "message": "", "image": None},
    ):
        resp = client.get(f"/api/v1/providers/{pid}/build-image/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "idle"


# ===========================================================================
# Provider API tests — DELETE /api/v1/providers/{id}/build-image/status
# ===========================================================================


def test_clear_build_image_status():
    """DELETE /providers/{id}/build-image/status clears build status."""
    pid = _create_provider(
        name=f"build-clear-{uuid.uuid4().hex[:8]}", provider_type="gcp"
    )
    with patch("app.services.image_builder_service.clear_build_status") as mock_clear:
        resp = client.delete(f"/api/v1/providers/{pid}/build-image/status")
    assert resp.status_code == 204
    mock_clear.assert_called_once_with(pid)


# ===========================================================================
# Provider helper function unit tests
# ===========================================================================


def test_build_provider_credentials_ec2():
    """_build_provider_credentials returns correct dict for ec2 type."""
    from app.api.providers import ProviderCreate, _build_provider_credentials

    body = ProviderCreate(
        name="test",
        type="ec2",
        access_key_id="AKIA_TEST",
        secret_access_key="SECRET_TEST",
    )
    provider = Provider(name="test", type="ec2", created_by="test@test")
    creds = _build_provider_credentials(body, provider)
    assert creds["access_key_id"] == "AKIA_TEST"
    assert creds["secret_access_key"] == "SECRET_TEST"


def test_build_provider_credentials_s3_with_bucket():
    """_build_provider_credentials includes bucket and endpoint for s3 type."""
    from app.api.providers import ProviderCreate, _build_provider_credentials

    body = ProviderCreate(
        name="test",
        type="s3",
        access_key_id="AKIA",
        secret_access_key="SEC",
        bucket="my-bucket",
        endpoint_url="https://s3.example.com",
    )
    provider = Provider(name="test", type="s3", created_by="test@test")
    creds = _build_provider_credentials(body, provider)
    assert creds["bucket"] == "my-bucket"
    assert creds["endpoint_url"] == "https://s3.example.com"


def test_build_provider_credentials_ocpvirt():
    """_build_provider_credentials returns cluster creds for ocpvirt."""
    from app.api.providers import ProviderCreate, _build_provider_credentials

    body = ProviderCreate(
        name="test",
        type="ocpvirt",
        api_url="https://api.cluster.example.com:6443",
        token="sha256~tok",
        namespace="troshka",
        iso_pvc="rhel-iso",
    )
    provider = Provider(name="test", type="ocpvirt", created_by="test@test")
    creds = _build_provider_credentials(body, provider)
    assert creds["api_url"] == "https://api.cluster.example.com:6443"
    assert creds["token"] == "sha256~tok"
    assert creds["namespace"] == "troshka"
    assert creds["iso_pvc"] == "rhel-iso"
    assert provider.default_region == "troshka"


def test_build_provider_credentials_kubevirt():
    """_build_provider_credentials returns cluster creds with cache/prefix for kubevirt."""
    from app.api.providers import ProviderCreate, _build_provider_credentials

    body = ProviderCreate(
        name="test",
        type="kubevirt",
        api_url="https://api.kv.example.com:6443",
        token="sha256~kvtok",
        namespace="my-operator-ns",
        cache_namespace="my-cache",
        project_prefix="proj-",
    )
    provider = Provider(name="test", type="kubevirt", created_by="test@test")
    creds = _build_provider_credentials(body, provider)
    assert creds["namespace"] == "my-operator-ns"
    assert creds["cache_namespace"] == "my-cache"
    assert creds["project_prefix"] == "proj-"
    assert provider.default_region == "my-operator-ns"


def test_build_provider_credentials_gcp():
    """_build_provider_credentials parses JSON and sets gcp_project_id."""
    from app.api.providers import ProviderCreate, _build_provider_credentials

    sa_json_str = '{"type":"service_account","project_id":"p"}'
    body = ProviderCreate(
        name="test",
        type="gcp",
        gcp_project_id="my-gcp-proj",
        service_account_json=sa_json_str,
    )
    provider = Provider(name="test", type="gcp", created_by="test@test")
    creds = _build_provider_credentials(body, provider)
    assert creds["service_account_json"] == {
        "type": "service_account",
        "project_id": "p",
    }
    assert provider.gcp_project_id == "my-gcp-proj"


def test_build_provider_credentials_azure():
    """_build_provider_credentials returns Azure SP creds and sets fields."""
    from app.api.providers import ProviderCreate, _build_provider_credentials

    body = ProviderCreate(
        name="test",
        type="azure",
        azure_tenant_id="t-id",
        azure_client_id="c-id",
        azure_client_secret="c-secret",
        azure_subscription_id="s-id",
        azure_location="westus2",
    )
    provider = Provider(name="test", type="azure", created_by="test@test")
    creds = _build_provider_credentials(body, provider)
    assert creds["tenant_id"] == "t-id"
    assert creds["client_id"] == "c-id"
    assert creds["client_secret"] == "c-secret"
    assert creds["subscription_id"] == "s-id"
    assert provider.azure_subscription_id == "s-id"
    assert provider.azure_location == "westus2"


def test_build_provider_credentials_unknown_type():
    """_build_provider_credentials raises HTTPException for unknown type."""
    from fastapi import HTTPException as _HTTPException

    from app.api.providers import ProviderCreate, _build_provider_credentials

    body = ProviderCreate(name="test", type="linode")
    provider = Provider(name="test", type="linode", created_by="test@test")
    try:
        _build_provider_credentials(body, provider)
        assert False, "Should have raised HTTPException"
    except _HTTPException as e:
        assert e.status_code == 400
        assert "Unknown provider type" in e.detail


def test_build_provider_response_shape():
    """_build_provider_response returns ProviderResponse with all expected fields."""
    import datetime

    from app.api.providers import _build_provider_response

    provider = Provider(
        id=str(uuid.uuid4()),
        name="test-resp",
        type="ec2",
        state="active",
        default_region="us-east-1",
        default_image="ami-123",
        vpc_id="vpc-abc",
        subnet_id="subnet-def",
        security_group_id="sg-ghi",
        created_by="test@test",
        created_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
    )
    # Simulate no hosts and no credentials
    provider.hosts = []
    provider.credentials = None

    resp = _build_provider_response(provider, host_count=0)
    assert resp.id == provider.id
    assert resp.name == "test-resp"
    assert resp.type == "ec2"
    assert resp.state == "active"
    assert resp.default_region == "us-east-1"
    assert resp.default_image == "ami-123"
    assert resp.vpc_id == "vpc-abc"
    assert resp.subnet_id == "subnet-def"
    assert resp.security_group_id == "sg-ghi"
    assert resp.host_count == 0
    assert resp.has_credentials is False
    assert resp.console_configured is False
    assert resp.created_at == "2025-01-01T00:00:00+00:00"


def test_build_provider_response_with_credentials():
    """_build_provider_response detects credentials and endpoint_url."""
    import datetime
    import json

    from app.api.providers import _build_provider_response

    provider = Provider(
        id=str(uuid.uuid4()),
        name="test-creds",
        type="s3",
        state="active",
        created_by="test@test",
        created_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
    )
    provider.hosts = []
    provider.credentials = json.dumps(
        {
            "access_key_id": "AKIA",
            "secret_access_key": "SEC",
            "endpoint_url": "https://s3.custom.com",
        }
    )

    resp = _build_provider_response(provider, host_count=0)
    assert resp.has_credentials is True
    assert resp.endpoint_url == "https://s3.custom.com"


def test_build_provider_response_console_configured():
    """_build_provider_response detects console configuration."""
    import datetime

    from app.api.providers import _build_provider_response

    provider = Provider(
        id=str(uuid.uuid4()),
        name="test-console",
        type="ec2",
        state="active",
        console_zone_id="Z123456",
        console_base_domain="console.example.com",
        console_nameservers=["ns1.example.com"],
        created_by="test@test",
        created_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
    )
    provider.hosts = []
    provider.credentials = None

    resp = _build_provider_response(provider, host_count=0)
    assert resp.console_configured is True
    assert resp.console_base_domain == "console.example.com"
    assert resp.console_nameservers == ["ns1.example.com"]


# ===========================================================================
# Provider API tests — setup_console for EC2 (Route53 + IAM)
# ===========================================================================


def test_setup_console_ec2_creates_zone():
    """POST setup-console for EC2 creates Route53 zone and IAM resources."""
    pid = _create_provider(name=f"console-ec2-new-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    db.commit()
    db.close()

    mock_r53 = MagicMock()
    mock_r53.list_hosted_zones_by_name.return_value = {"HostedZones": []}
    mock_r53.create_hosted_zone.return_value = {
        "HostedZone": {"Id": "/hostedzone/Z1234567890"},
        "DelegationSet": {"NameServers": ["ns-1.aws.com", "ns-2.aws.com"]},
    }

    mock_iam = MagicMock()
    mock_iam.exceptions.EntityAlreadyExistsException = type(
        "EntityAlreadyExistsException", (Exception,), {}
    )
    mock_iam.exceptions.LimitExceededException = type(
        "LimitExceededException", (Exception,), {}
    )

    def boto_client_factory(service, **kwargs):
        if service == "route53":
            return mock_r53
        if service == "iam":
            return mock_iam
        return MagicMock()

    with patch("boto3.client", side_effect=boto_client_factory):
        resp = client.post(
            f"/api/v1/providers/{pid}/setup-console",
            json={"base_domain": "vnc.example.com"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["zone_id"] == "Z1234567890"
    assert data["base_domain"] == "vnc.example.com"
    assert data["nameservers"] == ["ns-1.aws.com", "ns-2.aws.com"]
    mock_r53.create_hosted_zone.assert_called_once()
    mock_iam.create_role.assert_called_once()
    mock_iam.create_instance_profile.assert_called_once()


def test_setup_console_ec2_zone_already_exists():
    """POST setup-console for EC2 reuses existing Route53 zone."""
    pid = _create_provider(name=f"console-ec2-exist-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    db.commit()
    db.close()

    mock_r53 = MagicMock()
    mock_r53.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Name": "vnc.example.com.", "Id": "/hostedzone/ZEXISTING"}]
    }
    mock_r53.get_hosted_zone.return_value = {
        "DelegationSet": {"NameServers": ["ns-exist.aws.com"]}
    }

    mock_iam = MagicMock()
    mock_iam.exceptions.EntityAlreadyExistsException = type(
        "EntityAlreadyExistsException", (Exception,), {}
    )
    mock_iam.exceptions.LimitExceededException = type(
        "LimitExceededException", (Exception,), {}
    )

    def boto_client_factory(service, **kwargs):
        if service == "route53":
            return mock_r53
        if service == "iam":
            return mock_iam
        return MagicMock()

    with patch("boto3.client", side_effect=boto_client_factory):
        resp = client.post(
            f"/api/v1/providers/{pid}/setup-console",
            json={"base_domain": "vnc.example.com"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["zone_id"] == "ZEXISTING"
    mock_r53.create_hosted_zone.assert_not_called()


def test_setup_console_ec2_iam_entity_already_exists():
    """POST setup-console handles EntityAlreadyExistsException for IAM resources."""
    pid = _create_provider(name=f"console-ec2-iam-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    db.commit()
    db.close()

    mock_r53 = MagicMock()
    mock_r53.list_hosted_zones_by_name.return_value = {"HostedZones": []}
    mock_r53.create_hosted_zone.return_value = {
        "HostedZone": {"Id": "/hostedzone/ZNEW"},
        "DelegationSet": {"NameServers": ["ns1.aws.com"]},
    }

    entity_exists = type("EntityAlreadyExistsException", (Exception,), {})
    limit_exceeded = type("LimitExceededException", (Exception,), {})
    mock_iam = MagicMock()
    mock_iam.exceptions.EntityAlreadyExistsException = entity_exists
    mock_iam.exceptions.LimitExceededException = limit_exceeded
    mock_iam.create_role.side_effect = entity_exists("Role exists")
    mock_iam.create_instance_profile.side_effect = entity_exists("Profile exists")
    mock_iam.add_role_to_instance_profile.side_effect = limit_exceeded("Already added")

    def boto_client_factory(service, **kwargs):
        if service == "route53":
            return mock_r53
        if service == "iam":
            return mock_iam
        return MagicMock()

    with patch("boto3.client", side_effect=boto_client_factory):
        resp = client.post(
            f"/api/v1/providers/{pid}/setup-console",
            json={"base_domain": "vnc2.example.com"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["zone_id"] == "ZNEW"
    mock_iam.put_role_policy.assert_called_once()


def test_setup_console_ec2_failure():
    """POST setup-console returns 500 on unexpected boto error."""
    pid = _create_provider(name=f"console-ec2-fail-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    db.commit()
    db.close()

    with patch("boto3.client", side_effect=Exception("AWS error")):
        resp = client.post(
            f"/api/v1/providers/{pid}/setup-console",
            json={"base_domain": "fail.example.com"},
        )
    assert resp.status_code == 500


# ===========================================================================
# Provider API tests — delete_console with zone configured
# ===========================================================================


def test_delete_console_with_zone():
    """DELETE console for provider with zone calls Route53 cleanup."""
    pid = _create_provider(name=f"del-console-zone-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.console_zone_id = "Z_TO_DELETE"
    p.console_base_domain = "vnc.example.com"
    p.console_nameservers = ["ns1.example.com"]
    db.commit()
    db.close()

    with patch(
        "app.api.providers._delete_hosted_zone_if_unused"
    ) as mock_zone_del, patch("app.api.providers._clear_console_config") as mock_clear:
        resp = client.delete(f"/api/v1/providers/{pid}/console")
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"
    mock_zone_del.assert_called_once()
    mock_clear.assert_called_once()


# ===========================================================================
# Provider helper tests — _delete_dns_records
# ===========================================================================


def test_delete_dns_records_with_records():
    """_delete_dns_records deletes A and CNAME records via paginator."""
    from app.api.providers import _delete_dns_records

    mock_r53 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "ResourceRecordSets": [
                {
                    "Name": "host1.vnc.example.com.",
                    "Type": "A",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": "1.2.3.4"}],
                },
                {
                    "Name": "vnc.example.com.",
                    "Type": "NS",
                    "TTL": 300,
                    "ResourceRecords": [],
                },
                {
                    "Name": "cname.vnc.example.com.",
                    "Type": "CNAME",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": "other.example.com"}],
                },
            ]
        }
    ]
    mock_r53.get_paginator.return_value = paginator

    _delete_dns_records(mock_r53, "Z_TEST")

    mock_r53.change_resource_record_sets.assert_called_once()
    call_args = mock_r53.change_resource_record_sets.call_args
    changes = call_args[1]["ChangeBatch"]["Changes"]
    assert len(changes) == 2
    types = {c["ResourceRecordSet"]["Type"] for c in changes}
    assert types == {"A", "CNAME"}


def test_delete_dns_records_no_records():
    """_delete_dns_records does nothing when no A/CNAME records exist."""
    from app.api.providers import _delete_dns_records

    mock_r53 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "ResourceRecordSets": [
                {"Name": "x.", "Type": "NS", "TTL": 300, "ResourceRecords": []},
                {"Name": "x.", "Type": "SOA", "TTL": 300, "ResourceRecords": []},
            ]
        }
    ]
    mock_r53.get_paginator.return_value = paginator

    _delete_dns_records(mock_r53, "Z_TEST")

    mock_r53.change_resource_record_sets.assert_not_called()


# ===========================================================================
# Provider helper tests — _delete_hosted_zone_if_unused
# ===========================================================================


def test_delete_hosted_zone_if_unused_deletes():
    """_delete_hosted_zone_if_unused deletes zone when no other providers use it."""
    from app.api.providers import _delete_hosted_zone_if_unused

    pid = _create_provider(
        name=f"zone-unused-{uuid.uuid4().hex[:8]}",
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.console_zone_id = "Z_SOLE_USER"
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    db.commit()

    mock_r53 = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"ResourceRecordSets": []}]
    mock_r53.get_paginator.return_value = mock_paginator

    with patch("boto3.client", return_value=mock_r53):
        _delete_hosted_zone_if_unused(db, pid, "Z_SOLE_USER", p.get_credentials())

    mock_r53.delete_hosted_zone.assert_called_once_with(Id="Z_SOLE_USER")
    db.close()


def test_delete_hosted_zone_if_unused_keeps_shared():
    """_delete_hosted_zone_if_unused keeps zone when other providers share it."""
    from app.api.providers import _delete_hosted_zone_if_unused

    pid1 = _create_provider(name=f"zone-share1-{uuid.uuid4().hex[:8]}")
    pid2 = _create_provider(name=f"zone-share2-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p1 = db.query(Provider).filter_by(id=pid1).first()
    p2 = db.query(Provider).filter_by(id=pid2).first()
    p1.console_zone_id = "Z_SHARED"
    p2.console_zone_id = "Z_SHARED"
    p1.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    db.commit()

    with patch("boto3.client") as mock_boto:
        _delete_hosted_zone_if_unused(db, pid1, "Z_SHARED", p1.get_credentials())
    mock_boto.assert_not_called()
    db.close()


# ===========================================================================
# Provider helper tests — _clear_console_config
# ===========================================================================


def test_clear_console_config():
    """_clear_console_config clears provider and host console_domain fields."""
    from app.api.providers import _clear_console_config
    from app.models.host import Host

    pid = _create_provider(name=f"clear-console-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.console_zone_id = "Z_CLEAR"
    p.console_base_domain = "vnc.example.com"
    p.console_nameservers = ["ns1.example.com"]

    host = Host(
        id=str(uuid.uuid4()),
        provider_id=pid,
        instance_id="i-fake",
        instance_type="m5.xlarge",
        region="us-east-1",
        state="active",
        ip_address="1.2.3.4",
        agent_status="connected",
        total_vcpus=4,
        total_ram_mb=16384,
        storage_size_gb=100,
        max_eips=5,
        console_domain="host1.vnc.example.com",
    )
    db.add(host)
    db.commit()

    _clear_console_config(db, p)
    db.commit()

    db.refresh(p)
    db.refresh(host)
    assert p.console_zone_id is None
    assert p.console_base_domain is None
    assert p.console_nameservers is None
    assert host.console_domain is None
    db.close()


# ===========================================================================
# Provider API tests — discover_images for EC2
# ===========================================================================


def test_discover_images_ec2_success():
    """GET discover-images returns RHEL images for EC2 provider."""
    pid = _create_provider(name=f"disc-img-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_ec2 = MagicMock()
    mock_ec2.describe_images.return_value = {
        "Images": [
            {
                "ImageId": "ami-111",
                "Name": "RHEL-9.5.0_HVM-20250101-x86_64-0-Access2-GP3",
                "CreationDate": "2025-01-01T00:00:00.000Z",
            },
            {
                "ImageId": "ami-222",
                "Name": "RHEL-9.7.0_HVM-20250601-x86_64-0-Access2-GP3",
                "CreationDate": "2025-06-01T00:00:00.000Z",
            },
        ]
    }

    with patch("boto3.client", return_value=mock_ec2):
        resp = client.get(f"/api/v1/providers/{pid}/discover-images")
    assert resp.status_code == 200
    data = resp.json()
    assert data["region"] == "us-east-1"
    assert len(data["images"]) > 0
    img = data["images"][0]
    assert "image_id" in img
    assert "label" in img


def test_discover_images_ec2_not_found():
    """GET discover-images returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/providers/{fake_id}/discover-images")
    assert resp.status_code == 404


def test_discover_images_ec2_failure():
    """GET discover-images returns 500 on boto error."""
    pid = _create_provider(name=f"disc-img-fail-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    with patch("boto3.client", side_effect=Exception("AWS error")):
        resp = client.get(f"/api/v1/providers/{pid}/discover-images")
    assert resp.status_code == 500


def test_discover_images_ec2_no_matching_images():
    """GET discover-images returns empty images list when no AMIs match."""
    pid = _create_provider(name=f"disc-img-empty-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_ec2 = MagicMock()
    mock_ec2.describe_images.return_value = {"Images": []}

    with patch("boto3.client", return_value=mock_ec2):
        resp = client.get(f"/api/v1/providers/{pid}/discover-images")
    assert resp.status_code == 200
    data = resp.json()
    assert data["images"] == []


# ===========================================================================
# Provider API tests — discover_vpcs
# ===========================================================================


def test_discover_vpcs_success():
    """GET discover-vpcs returns VPCs for EC2 provider."""
    pid = _create_provider(name=f"disc-vpc-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_ec2 = MagicMock()
    mock_ec2.describe_vpcs.return_value = {
        "Vpcs": [
            {
                "VpcId": "vpc-111",
                "CidrBlock": "10.100.0.0/16",
                "Tags": [{"Key": "Name", "Value": "troshka-vpc"}],
                "IsDefault": False,
            }
        ]
    }
    mock_ec2.describe_subnets.return_value = {
        "Subnets": [
            {
                "SubnetId": "subnet-aaa",
                "AvailabilityZone": "us-east-1a",
                "CidrBlock": "10.100.1.0/24",
                "MapPublicIpOnLaunch": True,
            }
        ]
    }

    with patch("boto3.client", return_value=mock_ec2):
        resp = client.get(f"/api/v1/providers/{pid}/discover-vpcs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["region"] == "us-east-1"
    assert len(data["vpcs"]) == 1
    vpc = data["vpcs"][0]
    assert vpc["vpc_id"] == "vpc-111"
    assert vpc["name"] == "troshka-vpc"
    assert len(vpc["subnets"]) == 1


def test_discover_vpcs_not_found():
    """GET discover-vpcs returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/providers/{fake_id}/discover-vpcs")
    assert resp.status_code == 404


def test_discover_vpcs_failure():
    """GET discover-vpcs returns 500 on boto error."""
    pid = _create_provider(name=f"disc-vpc-fail-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    with patch("boto3.client", side_effect=Exception("AWS error")):
        resp = client.get(f"/api/v1/providers/{pid}/discover-vpcs")
    assert resp.status_code == 500


def test_discover_vpcs_vpc_without_name_tag():
    """GET discover-vpcs uses vpc_id as name when Name tag is missing."""
    pid = _create_provider(name=f"disc-vpc-notag-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-west-2"
    db.commit()
    db.close()

    mock_ec2 = MagicMock()
    mock_ec2.describe_vpcs.return_value = {
        "Vpcs": [
            {
                "VpcId": "vpc-notag",
                "CidrBlock": "10.0.0.0/16",
            }
        ]
    }
    mock_ec2.describe_subnets.return_value = {"Subnets": []}

    with patch("boto3.client", return_value=mock_ec2):
        resp = client.get(f"/api/v1/providers/{pid}/discover-vpcs")
    assert resp.status_code == 200
    vpc = resp.json()["vpcs"][0]
    assert vpc["name"] == "vpc-notag"


# ===========================================================================
# Provider API tests — create_vpc
# ===========================================================================


def test_create_vpc_success():
    """POST create-vpc creates a full VPC with subnets and security group."""
    pid = _create_provider(name=f"create-vpc-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_ec2 = MagicMock()
    mock_ec2.create_vpc.return_value = {"Vpc": {"VpcId": "vpc-new123"}}
    mock_ec2.create_internet_gateway.return_value = {
        "InternetGateway": {"InternetGatewayId": "igw-abc"}
    }
    mock_ec2.describe_availability_zones.return_value = {
        "AvailabilityZones": [
            {"ZoneName": "us-east-1a"},
            {"ZoneName": "us-east-1b"},
        ]
    }
    mock_ec2.create_subnet.side_effect = [
        {"Subnet": {"SubnetId": "subnet-1a"}},
        {"Subnet": {"SubnetId": "subnet-1b"}},
    ]
    mock_ec2.describe_route_tables.return_value = {
        "RouteTables": [{"RouteTableId": "rtb-main"}]
    }

    with patch("boto3.client", return_value=mock_ec2), patch(
        "app.services.provisioner.ensure_security_group", return_value="sg-new"
    ):
        resp = client.post(f"/api/v1/providers/{pid}/create-vpc")
    assert resp.status_code == 200
    data = resp.json()
    assert data["vpc_id"] == "vpc-new123"
    assert data["security_group_id"] == "sg-new"
    assert len(data["subnet_ids"]) == 2
    assert data["availability_zones"] == ["us-east-1a", "us-east-1b"]


def test_create_vpc_not_found():
    """POST create-vpc returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/providers/{fake_id}/create-vpc")
    assert resp.status_code == 404


def test_create_vpc_failure():
    """POST create-vpc returns 500 on boto error."""
    pid = _create_provider(name=f"create-vpc-fail-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    with patch("boto3.client", side_effect=Exception("VPC create error")):
        resp = client.post(f"/api/v1/providers/{pid}/create-vpc")
    assert resp.status_code == 500


# ===========================================================================
# Provider API tests — setup_infrastructure
# ===========================================================================


def test_setup_infrastructure_success():
    """POST setup-infra sets VPC/subnet/SG on provider."""
    pid = _create_provider(name=f"setup-infra-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    db.commit()
    db.close()

    with patch(
        "app.services.provisioner.ensure_security_group", return_value="sg-infra"
    ):
        resp = client.post(
            f"/api/v1/providers/{pid}/setup-infra",
            params={"vpc_id": "vpc-infra", "subnet_id": "subnet-infra"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vpc_id"] == "vpc-infra"
    assert data["subnet_id"] == "subnet-infra"
    assert data["security_group_id"] == "sg-infra"


def test_setup_infrastructure_not_found():
    """POST setup-infra returns 404 for nonexistent provider."""
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/providers/{fake_id}/setup-infra",
        params={"vpc_id": "vpc-x", "subnet_id": "subnet-x"},
    )
    assert resp.status_code == 404


def test_setup_infrastructure_failure():
    """POST setup-infra returns 500 on ensure_security_group error."""
    pid = _create_provider(name=f"setup-infra-fail-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    db.commit()
    db.close()

    with patch(
        "app.services.provisioner.ensure_security_group",
        side_effect=Exception("SG error"),
    ):
        resp = client.post(
            f"/api/v1/providers/{pid}/setup-infra",
            params={"vpc_id": "vpc-x", "subnet_id": "subnet-x"},
        )
    assert resp.status_code == 500


# ===========================================================================
# Provider API tests — test_provider for S3
# ===========================================================================


def test_test_provider_s3_bucket_exists():
    """POST test for S3 provider with existing bucket returns ok."""
    pid = _create_provider(
        name=f"test-s3-ok-{uuid.uuid4().hex[:8]}", provider_type="s3"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "access_key_id": "AKIA_FAKE",
            "secret_access_key": "secret_fake",
            "bucket": "my-bucket",
        }
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_s3 = MagicMock()
    mock_s3.head_bucket.return_value = {}
    mock_s3.exceptions.ClientError = Exception

    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/test",
    }

    def boto_factory(service, **kwargs):
        if service == "s3":
            return mock_s3
        if service == "sts":
            return mock_sts
        return MagicMock()

    with patch("boto3.client", side_effect=boto_factory):
        resp = client.post(f"/api/v1/providers/{pid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["bucket"] == "my-bucket"
    assert data["account"] == "123456789012"


def test_test_provider_s3_bucket_missing():
    """POST test for S3 provider with missing bucket returns bucket_missing."""
    from botocore.exceptions import ClientError

    pid = _create_provider(
        name=f"test-s3-miss-{uuid.uuid4().hex[:8]}", provider_type="s3"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "access_key_id": "AKIA_FAKE",
            "secret_access_key": "secret_fake",
            "bucket": "missing-bucket",
        }
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
    client_error = ClientError(error_response, "HeadBucket")

    mock_s3 = MagicMock()
    mock_s3.head_bucket.side_effect = client_error
    mock_s3.exceptions.ClientError = ClientError

    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/test",
    }

    def boto_factory(service, **kwargs):
        if service == "s3":
            return mock_s3
        if service == "sts":
            return mock_sts
        return MagicMock()

    with patch("boto3.client", side_effect=boto_factory):
        resp = client.post(f"/api/v1/providers/{pid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["bucket_missing"] is True


def test_test_provider_s3_bucket_denied():
    """POST test for S3 provider with access denied returns bucket_denied."""
    from botocore.exceptions import ClientError

    pid = _create_provider(
        name=f"test-s3-deny-{uuid.uuid4().hex[:8]}", provider_type="s3"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "access_key_id": "AKIA_FAKE",
            "secret_access_key": "secret_fake",
            "bucket": "denied-bucket",
        }
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    error_response = {"Error": {"Code": "403", "Message": "Forbidden"}}
    client_error = ClientError(error_response, "HeadBucket")

    mock_s3 = MagicMock()
    mock_s3.head_bucket.side_effect = client_error
    mock_s3.exceptions.ClientError = ClientError

    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/test",
    }

    def boto_factory(service, **kwargs):
        if service == "s3":
            return mock_s3
        if service == "sts":
            return mock_sts
        return MagicMock()

    with patch("boto3.client", side_effect=boto_factory):
        resp = client.post(f"/api/v1/providers/{pid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["bucket_denied"] is True


# ===========================================================================
# Provider API tests — test_provider for OCP Virt
# ===========================================================================


def test_test_provider_ocpvirt_success():
    """POST test for OCP Virt provider with mocked k8s clients."""
    pid = _create_provider(
        name=f"test-ocpv-ok-{uuid.uuid4().hex[:8]}", provider_type="ocpvirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "api_url": "https://api.cluster.test:6443",
            "token": "tok",
            "namespace": "troshka",
        }
    )
    db.commit()
    db.close()

    mock_core_api = MagicMock()
    mock_core_api.read_namespace.return_value = MagicMock()
    mock_node1 = MagicMock()
    mock_node2 = MagicMock()
    mock_core_api.list_node.return_value = MagicMock(items=[mock_node1, mock_node2])

    with patch(
        "app.services.providers.ocpvirt._get_k8s_clients",
        return_value=(MagicMock(), mock_core_api),
    ):
        resp = client.post(f"/api/v1/providers/{pid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["nodes"] == 2
    assert data["namespace"] == "troshka"


# ===========================================================================
# Provider API tests — PATCH credentials update paths
# ===========================================================================


def test_update_provider_cluster_credentials():
    """PATCH with api_url/token updates cluster provider credentials."""
    pid = _create_provider(
        name=f"upd-cluster-{uuid.uuid4().hex[:8]}", provider_type="ocpvirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"api_url": "https://api.old:6443", "token": "old-tok", "namespace": "troshka"}
    )
    db.commit()
    db.close()

    resp = client.patch(
        f"/api/v1/providers/{pid}",
        json={
            "api_url": "https://api.new:6443",
            "token": "new-tok",
            "namespace": "new-ns",
        },
    )
    assert resp.status_code == 200

    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    creds = p.get_credentials()
    assert creds["api_url"] == "https://api.new:6443"
    assert creds["token"] == "new-tok"
    assert creds["namespace"] == "new-ns"
    assert p.default_region == "new-ns"
    db.close()


def test_update_provider_aws_credentials():
    """PATCH with access_key_id/secret updates AWS provider credentials."""
    pid = _create_provider(name=f"upd-aws-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials({"access_key_id": "OLD_AKIA", "secret_access_key": "OLD_SECRET"})
    db.commit()
    db.close()

    resp = client.patch(
        f"/api/v1/providers/{pid}",
        json={
            "access_key_id": "NEW_AKIA",
            "secret_access_key": "NEW_SECRET",
        },
    )
    assert resp.status_code == 200

    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    creds = p.get_credentials()
    assert creds["access_key_id"] == "NEW_AKIA"
    assert creds["secret_access_key"] == "NEW_SECRET"
    db.close()


def test_update_provider_cache_and_prefix():
    """PATCH with cache_namespace/project_prefix updates kubevirt credentials."""
    pid = _create_provider(
        name=f"upd-kv-cache-{uuid.uuid4().hex[:8]}", provider_type="kubevirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "api_url": "https://api.kv:6443",
            "token": "tok",
            "namespace": "troshka-operator",
            "cache_namespace": "old-cache",
            "project_prefix": "old-",
        }
    )
    db.commit()
    db.close()

    resp = client.patch(
        f"/api/v1/providers/{pid}",
        json={
            "namespace": "new-operator-ns",
            "cache_namespace": "new-cache",
            "project_prefix": "new-",
        },
    )
    assert resp.status_code == 200

    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    creds = p.get_credentials()
    assert creds["cache_namespace"] == "new-cache"
    assert creds["project_prefix"] == "new-"
    assert creds["namespace"] == "new-operator-ns"
    db.close()


# ===========================================================================
# Provider API tests — delete_provider for kubevirt type
# ===========================================================================


def test_delete_kubevirt_provider():
    """DELETE kubevirt provider calls K8s cleanup and removes DB resources."""
    pid = _create_provider(
        name=f"del-kv-{uuid.uuid4().hex[:8]}", provider_type="kubevirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "api_url": "https://api.kv:6443",
            "token": "tok",
            "namespace": "troshka-operator",
            "cache_namespace": "troshka-cache",
            "project_prefix": "troshka-",
        }
    )
    db.commit()
    db.close()

    with patch(
        "app.api.providers._cleanup_kubevirt_k8s_resources", return_value=MagicMock()
    ) as mock_k8s, patch("app.api.providers._cleanup_kubevirt_db_resources") as mock_db:
        resp = client.delete(f"/api/v1/providers/{pid}")
    assert resp.status_code == 204
    mock_k8s.assert_called_once()
    mock_db.assert_called_once()


# ===========================================================================
# Provider helper function tests — GCP image helpers
# ===========================================================================


def test_is_valid_gcp_image_valid():
    """_is_valid_gcp_image accepts valid RHEL 9/10 LVM images."""
    from app.api.providers import _is_valid_gcp_image

    img = MagicMock()
    img.name = "rhel-9-lvm-20250101"
    img.deprecated = None
    assert _is_valid_gcp_image(img, ("arm64", "sap")) is True


def test_is_valid_gcp_image_wrong_prefix():
    """_is_valid_gcp_image rejects images not starting with rhel-9 or rhel-10."""
    from app.api.providers import _is_valid_gcp_image

    img = MagicMock()
    img.name = "rhel-8-lvm-20250101"
    img.deprecated = None
    assert _is_valid_gcp_image(img, ()) is False


def test_is_valid_gcp_image_no_lvm():
    """_is_valid_gcp_image rejects images without lvm in name."""
    from app.api.providers import _is_valid_gcp_image

    img = MagicMock()
    img.name = "rhel-9-raw-20250101"
    img.deprecated = None
    assert _is_valid_gcp_image(img, ()) is False


def test_is_valid_gcp_image_skip_term():
    """_is_valid_gcp_image rejects images containing skip terms."""
    from app.api.providers import _is_valid_gcp_image

    img = MagicMock()
    img.name = "rhel-9-lvm-arm64-20250101"
    img.deprecated = None
    assert _is_valid_gcp_image(img, ("arm64",)) is False


def test_is_valid_gcp_image_deprecated():
    """_is_valid_gcp_image rejects deprecated images."""
    from app.api.providers import _is_valid_gcp_image

    img = MagicMock()
    img.name = "rhel-10-lvm-20250101"
    img.deprecated = MagicMock()
    img.deprecated.state = "DEPRECATED"
    assert _is_valid_gcp_image(img, ()) is False


def test_build_gcp_image_prefix_with_version():
    """_build_gcp_image_prefix splits on version suffix."""
    from app.api.providers import _build_gcp_image_prefix

    result = _build_gcp_image_prefix("rhel-9-lvm-v20250601", "PAYG")
    assert result == "PAYG:rhel-9-lvm"


def test_build_gcp_image_prefix_without_version():
    """_build_gcp_image_prefix handles names without -v suffix."""
    from app.api.providers import _build_gcp_image_prefix

    result = _build_gcp_image_prefix("rhel-10-lvm-custom", "BYOS")
    assert result == "BYOS:rhel-10-lvm-custom"


# ===========================================================================
# Provider helper function tests — Azure image helpers
# ===========================================================================


def test_is_valid_azure_sku_valid():
    """_is_valid_azure_sku accepts valid RHEL 9/10 LVM SKUs."""
    from app.api.providers import _is_valid_azure_sku

    assert _is_valid_azure_sku("9-lvm-gen2", []) is True
    assert _is_valid_azure_sku("rhel-lvm9-gen2", []) is True
    assert _is_valid_azure_sku("10-lvm-gen2", []) is True
    assert _is_valid_azure_sku("rhel-lvm10-gen2", []) is True


def test_is_valid_azure_sku_no_lvm():
    """_is_valid_azure_sku rejects SKUs without lvm."""
    from app.api.providers import _is_valid_azure_sku

    assert _is_valid_azure_sku("9-gen2", []) is False


def test_is_valid_azure_sku_wrong_prefix():
    """_is_valid_azure_sku rejects SKUs not matching known prefixes."""
    from app.api.providers import _is_valid_azure_sku

    assert _is_valid_azure_sku("8-lvm-gen2", []) is False


def test_is_valid_azure_sku_prefers_gen2():
    """_is_valid_azure_sku skips non-gen2 when gen2 variant exists."""
    from app.api.providers import _is_valid_azure_sku

    gen2_sku = MagicMock()
    gen2_sku.name = "9-lvm-gen2"
    assert _is_valid_azure_sku("9-lvm", [gen2_sku]) is False


def test_build_azure_image_result():
    """_build_azure_image_result builds correct URN and version info."""
    from app.api.providers import _build_azure_image_result

    latest = MagicMock()
    latest.name = "9.4.2025060101"

    result = _build_azure_image_result("redhat", "RHEL", "9-lvm-gen2", latest, "PAYG")
    assert result["urn"] == "redhat:RHEL:9-lvm-gen2:9.4.2025060101"
    assert result["source"] == "PAYG"
    assert result["rhel_version"] == "9"


def test_build_azure_image_result_rhel_prefix():
    """_build_azure_image_result extracts version from rhel-prefixed SKU."""
    from app.api.providers import _build_azure_image_result

    latest = MagicMock()
    latest.name = "10.0.2025050101"

    result = _build_azure_image_result(
        "redhat", "rhel-byos", "rhel-lvm10-gen2", latest, "BYOS"
    )
    assert result["name"] == "rhel-lvm10-gen2"
    assert result["rhel_version"] == "rhel-lvm10-gen2"


# ===========================================================================
# Provider API tests — availability-zones success
# ===========================================================================


def test_availability_zones_success():
    """GET availability-zones returns sorted AZ list for EC2 provider."""
    pid = _create_provider(name=f"az-ok-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-west-2"
    db.commit()
    db.close()

    mock_ec2 = MagicMock()
    mock_ec2.describe_availability_zones.return_value = {
        "AvailabilityZones": [
            {"ZoneName": "us-west-2c"},
            {"ZoneName": "us-west-2a"},
            {"ZoneName": "us-west-2b"},
        ]
    }

    with patch("boto3.client", return_value=mock_ec2):
        resp = client.get(f"/api/v1/providers/{pid}/availability-zones")
    assert resp.status_code == 200
    azs = resp.json()
    assert azs == ["us-west-2a", "us-west-2b", "us-west-2c"]


# ===========================================================================
# Provider API tests — create S3 bucket success paths
# ===========================================================================


def test_create_bucket_success_us_east_1():
    """POST create-bucket creates bucket in us-east-1 (no LocationConstraint)."""
    pid = _create_provider(name=f"bucket-ok-{uuid.uuid4().hex[:8]}", provider_type="s3")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "access_key_id": "AKIA_FAKE",
            "secret_access_key": "secret_fake",
            "bucket": "test-bucket",
        }
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_s3 = MagicMock()

    with patch("boto3.client", return_value=mock_s3):
        resp = client.post(f"/api/v1/providers/{pid}/create-bucket")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert data["bucket"] == "test-bucket"
    mock_s3.create_bucket.assert_called_once_with(Bucket="test-bucket")


def test_create_bucket_success_other_region():
    """POST create-bucket creates bucket with LocationConstraint for non-us-east-1."""
    pid = _create_provider(
        name=f"bucket-west-{uuid.uuid4().hex[:8]}", provider_type="s3"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "access_key_id": "AKIA_FAKE",
            "secret_access_key": "secret_fake",
            "bucket": "test-bucket-west",
        }
    )
    p.default_region = "us-west-2"
    db.commit()
    db.close()

    mock_s3 = MagicMock()

    with patch("boto3.client", return_value=mock_s3):
        resp = client.post(f"/api/v1/providers/{pid}/create-bucket")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    mock_s3.create_bucket.assert_called_once_with(
        Bucket="test-bucket-west",
        CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
    )


def test_create_bucket_already_exists():
    """POST create-bucket returns exists for BucketAlreadyOwnedByYou."""
    pid = _create_provider(
        name=f"bucket-dup-{uuid.uuid4().hex[:8]}", provider_type="s3"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "access_key_id": "AKIA_FAKE",
            "secret_access_key": "secret_fake",
            "bucket": "my-existing-bucket",
        }
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_s3 = MagicMock()
    bucket_exists_exc = type("BucketAlreadyOwnedByYou", (Exception,), {})
    mock_s3.exceptions.BucketAlreadyOwnedByYou = bucket_exists_exc
    mock_s3.create_bucket.side_effect = bucket_exists_exc("Already owned")

    with patch("boto3.client", return_value=mock_s3):
        resp = client.post(f"/api/v1/providers/{pid}/create-bucket")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "exists"


def test_create_bucket_uses_custom_endpoint_url():
    """POST create-bucket passes endpoint_url to boto3 for self-hosted S3 (e.g. MinIO)."""
    pid = _create_provider(
        name=f"bucket-minio-{uuid.uuid4().hex[:8]}", provider_type="s3"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "access_key_id": "minioadmin",
            "secret_access_key": "minioadmin",
            "bucket": "test-bucket",
            "endpoint_url": "http://192.168.124.1:9000",
        }
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_s3 = MagicMock()

    with patch("boto3.client", return_value=mock_s3) as mock_boto3_client:
        resp = client.post(f"/api/v1/providers/{pid}/create-bucket")
    assert resp.status_code == 200
    mock_boto3_client.assert_called_once_with(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        endpoint_url="http://192.168.124.1:9000",
    )


# ===========================================================================
# Provider helper tests — _cleanup_kubevirt_k8s_resources
# ===========================================================================


def test_cleanup_kubevirt_k8s_resources_success():
    """_cleanup_kubevirt_k8s_resources deletes operator, CRDs, and namespaces."""
    from app.api.providers import _cleanup_kubevirt_k8s_resources

    pid = _create_provider(
        name=f"cleanup-kv-{uuid.uuid4().hex[:8]}", provider_type="kubevirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {
            "api_url": "https://api.kv:6443",
            "token": "tok",
            "namespace": "test-operator",
            "cache_namespace": "test-cache",
        }
    )
    db.commit()

    mock_core = MagicMock()
    mock_custom = MagicMock()
    mock_api_client = MagicMock()
    mock_apps = MagicMock()
    mock_ext = MagicMock()

    with patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        return_value=(mock_custom, mock_core, mock_api_client),
    ), patch("kubernetes.client.AppsV1Api", return_value=mock_apps), patch(
        "kubernetes.client.ApiextensionsV1Api", return_value=mock_ext
    ):
        result = _cleanup_kubevirt_k8s_resources(p, p.get_credentials())

    assert result is mock_core
    mock_apps.delete_namespaced_deployment.assert_called_once()
    assert mock_ext.delete_custom_resource_definition.call_count == 3
    assert mock_core.delete_namespace.call_count == 2
    db.close()


def test_cleanup_kubevirt_k8s_resources_connection_failure():
    """_cleanup_kubevirt_k8s_resources returns None on connection failure."""
    from app.api.providers import _cleanup_kubevirt_k8s_resources

    pid = _create_provider(
        name=f"cleanup-kv-fail-{uuid.uuid4().hex[:8]}", provider_type="kubevirt"
    )
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"api_url": "https://api.kv:6443", "token": "tok", "namespace": "ns"}
    )
    db.commit()

    with patch(
        "app.services.providers.kubevirt._get_k8s_clients",
        side_effect=Exception("Connection refused"),
    ):
        result = _cleanup_kubevirt_k8s_resources(p, p.get_credentials())
    assert result is None
    db.close()


# ===========================================================================
# Provider helper tests — _cleanup_kubevirt_db_resources
# ===========================================================================


def test_cleanup_kubevirt_db_resources():
    """_cleanup_kubevirt_db_resources deletes hosts and projects."""
    from app.api.providers import _cleanup_kubevirt_db_resources
    from app.models.host import Host

    pid = _create_provider(
        name=f"cleanup-db-{uuid.uuid4().hex[:8]}", provider_type="kubevirt"
    )
    db = TestSession()
    host = Host(
        id=str(uuid.uuid4()),
        provider_id=pid,
        instance_id="cluster-api",
        instance_type="kubevirt-cluster",
        region="ns",
        state="active",
        host_type="kubevirt-cluster",
        ip_address="1.2.3.4",
        agent_status="connected",
        total_vcpus=0,
        total_ram_mb=0,
        storage_size_gb=0,
        max_eips=0,
    )
    db.add(host)
    db.commit()

    p = db.query(Provider).filter_by(id=pid).first()
    creds = {"namespace": "troshka-operator", "project_prefix": "troshka-"}

    mock_core = MagicMock()
    _cleanup_kubevirt_db_resources(db, p, creds, mock_core)
    db.commit()

    remaining_hosts = db.query(Host).filter_by(provider_id=pid).all()
    assert len(remaining_hosts) == 0
    db.close()


# ===========================================================================
# Provider API tests — discover-ami backward compat alias
# ===========================================================================


def test_discover_ami_alias():
    """GET discover-ami forwards to discover-images."""
    pid = _create_provider(name=f"disc-ami-{uuid.uuid4().hex[:8]}")
    db = TestSession()
    p = db.query(Provider).filter_by(id=pid).first()
    p.set_credentials(
        {"access_key_id": "AKIA_FAKE", "secret_access_key": "secret_fake"}
    )
    p.default_region = "us-east-1"
    db.commit()
    db.close()

    mock_ec2 = MagicMock()
    mock_ec2.describe_images.return_value = {"Images": []}

    with patch("boto3.client", return_value=mock_ec2):
        resp = client.get(f"/api/v1/providers/{pid}/discover-ami")
    assert resp.status_code == 200
    data = resp.json()
    assert "images" in data


# ===========================================================================
# Provider API tests — test_provider for unknown type
# ===========================================================================


def test_test_provider_unknown_type():
    """POST test for unknown provider type returns 400."""
    db = TestSession()
    p = Provider(
        id=str(uuid.uuid4()),
        name=f"test-unk-{uuid.uuid4().hex[:8]}",
        type="linode",
        state="active",
        created_by="local-dev@troshka",
    )
    p.set_credentials({"api_key": "fake"})
    db.add(p)
    db.commit()
    pid = p.id
    db.close()

    resp = client.post(f"/api/v1/providers/{pid}/test")
    assert resp.status_code == 400
    assert "Unknown provider type" in resp.json()["detail"]


# ===========================================================================
# Provider register — auto_provision_host flag (avoid duplicate ocpvirt host)
# ===========================================================================


def _ocpvirt_payload(name, **extra):
    return {
        "name": name,
        "type": "ocpvirt",
        "api_url": "https://api.test.example:6443",
        "token": "test-token",
        "namespace": "troshka",
        **extra,
    }


def test_ocpvirt_register_auto_provisions_host_by_default():
    """Registering an OCP Virt provider auto-provisions a host by default."""
    from unittest.mock import patch

    name = f"ocpv-auto-{uuid.uuid4().hex[:8]}"
    with patch("app.api.providers._enqueue_cluster_host_provision") as m:
        resp = client.post("/api/v1/providers/", json=_ocpvirt_payload(name))
    assert resp.status_code == 201, resp.text
    m.assert_called_once()


def test_ocpvirt_register_skips_host_when_auto_provision_false():
    """auto_provision_host=false must NOT auto-create a host (caller creates it)."""
    from unittest.mock import patch

    name = f"ocpv-noauto-{uuid.uuid4().hex[:8]}"
    with patch("app.api.providers._enqueue_cluster_host_provision") as m:
        resp = client.post(
            "/api/v1/providers/",
            json=_ocpvirt_payload(name, auto_provision_host=False),
        )
    assert resp.status_code == 201, resp.text
    m.assert_not_called()
