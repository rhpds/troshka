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
