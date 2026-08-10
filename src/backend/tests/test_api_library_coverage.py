"""Tests for library API coverage -- multipart upload, import, share, S3 scan."""

import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.library import Library, LibraryItem, LibraryShare
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
        lib = Library(id=str(uuid.uuid4()), type="personal", owner_id=user_id)
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


def _create_other_user_and_library():
    """Create a second user with their own library. Returns (user_id, lib_id)."""
    db = TestSession()
    other_email = f"other-{uuid.uuid4().hex[:8]}@test.com"
    other_user = User(
        id=str(uuid.uuid4()),
        email=other_email,
        display_name="Other User",
        role="user",
        auth_source="test",
    )
    db.add(other_user)
    db.flush()
    other_lib = Library(id=str(uuid.uuid4()), type="personal", owner_id=other_user.id)
    db.add(other_lib)
    db.commit()
    uid = other_user.id
    lid = other_lib.id
    db.close()
    return uid, lid


# ===========================================================================
# POST /{item_id}/upload-start
# ===========================================================================


@patch(
    "app.api.library.s3_storage._get_s3_config",
    return_value={"endpoint_url": "", "bucket": "test-bucket"},
)
@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_upload_start_success(mock_client_fn, mock_bucket, _mock_config):
    """POST /library/{item_id}/upload-start returns presigned mode, upload_id, and s3_key."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id, name=f"upl-start-{uuid.uuid4().hex[:8]}", state="pending"
    )

    mock_s3 = MagicMock()
    mock_s3.create_multipart_upload.return_value = {"UploadId": "test-upload-123"}
    mock_client_fn.return_value = mock_s3

    resp = client.post(f"/api/v1/library/{item_id}/upload-start")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "presigned"
    assert data["upload_id"] == "test-upload-123"
    assert "s3_key" in data
    assert item_id in data["s3_key"]


@patch(
    "app.api.library.s3_storage._get_s3_config",
    return_value={"endpoint_url": "http://minio:9000", "bucket": "test-bucket"},
)
def test_upload_start_proxy_mode(_mock_config):
    """POST /library/{item_id}/upload-start returns proxy mode when endpoint_url is set (MinIO/dev)."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id, name=f"upl-start-proxy-{uuid.uuid4().hex[:8]}", state="pending"
    )

    resp = client.post(f"/api/v1/library/{item_id}/upload-start")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "proxy"
    assert "s3_key" in data
    assert item_id in data["s3_key"]
    assert "upload_id" not in data


@patch(
    "app.api.library.s3_storage._get_s3_config",
    side_effect=ValueError(
        "No S3 provider configured. Add an S3 provider in Admin > Providers."
    ),
)
def test_upload_start_no_s3_configured_returns_friendly_400(_mock_config):
    """POST /library/{item_id}/upload-start returns 400 with a friendly detail
    (not an uncaught 500) when no S3 provider/config is available."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id, name=f"upl-start-noS3-{uuid.uuid4().hex[:8]}", state="pending"
    )

    resp = client.post(f"/api/v1/library/{item_id}/upload-start")
    assert resp.status_code == 400
    assert "S3" in resp.json()["detail"]


def test_upload_start_not_found():
    """POST /library/{item_id}/upload-start returns 404 for missing item."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/library/{fake_id}/upload-start")
    assert resp.status_code == 404


def test_upload_start_access_denied():
    """POST /library/{item_id}/upload-start returns 403 for other user's item."""
    _other_uid, other_lib_id = _create_other_user_and_library()
    item_id = _create_library_item(
        other_lib_id, name=f"upl-deny-{uuid.uuid4().hex[:8]}"
    )
    resp = client.post(f"/api/v1/library/{item_id}/upload-start")
    assert resp.status_code == 403


# ===========================================================================
# POST /{item_id}/upload-part-url
# ===========================================================================


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_upload_part_url_success(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/upload-part-url returns a presigned URL."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"part-url-{uuid.uuid4().hex[:8]}",
        s3_key="library/test/key.qcow2",
        state="uploading",
    )

    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://s3.example.com/presigned"
    mock_client_fn.return_value = mock_s3

    resp = client.post(
        f"/api/v1/library/{item_id}/upload-part-url",
        params={"upload_id": "upl-123", "part_number": 1},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "https://s3.example.com/presigned"
    assert data["part_number"] == 1


def test_upload_part_url_not_found():
    """POST /library/{item_id}/upload-part-url returns 404 for missing item."""
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/library/{fake_id}/upload-part-url",
        params={"upload_id": "x", "part_number": 1},
    )
    assert resp.status_code == 404


def test_upload_part_url_no_s3_key():
    """POST /library/{item_id}/upload-part-url returns 404 when item has no s3_key."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"part-nokey-{uuid.uuid4().hex[:8]}",
        s3_key=None,
        state="pending",
    )
    resp = client.post(
        f"/api/v1/library/{item_id}/upload-part-url",
        params={"upload_id": "x", "part_number": 1},
    )
    assert resp.status_code == 404


# ===========================================================================
# POST /{item_id}/upload-complete
# ===========================================================================


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_upload_complete_success(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/upload-complete completes multipart upload."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"upl-done-{uuid.uuid4().hex[:8]}",
        s3_key="library/test/complete.qcow2",
        state="uploading",
    )

    mock_s3 = MagicMock()
    mock_s3.complete_multipart_upload.return_value = {}
    mock_s3.head_object.return_value = {"ContentLength": 5242880}
    mock_client_fn.return_value = mock_s3

    resp = client.post(
        f"/api/v1/library/{item_id}/upload-complete",
        json={
            "upload_id": "upl-123",
            "parts": [{"part_number": 1, "etag": '"abc123"'}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    assert data["size_bytes"] == 5242880


def test_upload_complete_not_found():
    """POST /library/{item_id}/upload-complete returns 404 for missing item."""
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/library/{fake_id}/upload-complete",
        json={"upload_id": "x", "parts": []},
    )
    assert resp.status_code == 404


def test_upload_complete_access_denied():
    """POST /library/{item_id}/upload-complete returns 403 for other user's item."""
    _other_uid, other_lib_id = _create_other_user_and_library()
    item_id = _create_library_item(
        other_lib_id,
        name=f"upl-deny-{uuid.uuid4().hex[:8]}",
        s3_key="library/other/key.qcow2",
    )
    resp = client.post(
        f"/api/v1/library/{item_id}/upload-complete",
        json={"upload_id": "x", "parts": []},
    )
    assert resp.status_code == 403


def test_upload_complete_missing_s3_key():
    """POST /library/{item_id}/upload-complete returns 400 when no s3_key."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"upl-nokey-{uuid.uuid4().hex[:8]}",
        s3_key=None,
        state="uploading",
    )
    resp = client.post(
        f"/api/v1/library/{item_id}/upload-complete",
        json={"upload_id": "x", "parts": []},
    )
    assert resp.status_code == 400
    assert "No S3 key" in resp.json()["detail"]


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_upload_complete_failure(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/upload-complete returns 500 on S3 error."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"upl-fail-{uuid.uuid4().hex[:8]}",
        s3_key="library/test/fail.qcow2",
        state="uploading",
    )

    mock_s3 = MagicMock()
    mock_s3.complete_multipart_upload.side_effect = Exception("S3 error")
    mock_client_fn.return_value = mock_s3

    resp = client.post(
        f"/api/v1/library/{item_id}/upload-complete",
        json={
            "upload_id": "upl-fail",
            "parts": [{"part_number": 1, "etag": '"abc"'}],
        },
    )
    assert resp.status_code == 500
    assert "Upload completion failed" in resp.json()["detail"]

    # Verify item state set to error
    db = TestSession()
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    assert item.state == "error"
    db.close()


# ===========================================================================
# POST /{item_id}/finalize-seed
# ===========================================================================


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_finalize_seed_skip_copy(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/finalize-seed with skip_copy uses seed_key directly."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"seed-skip-{uuid.uuid4().hex[:8]}",
        state="pending",
    )

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {"ContentLength": 2048}
    mock_client_fn.return_value = mock_s3

    resp = client.post(
        f"/api/v1/library/{item_id}/finalize-seed",
        json={"seed_key": "seeds/test.qcow2", "skip_copy": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    assert data["s3_key"] == "seeds/test.qcow2"
    assert data["size_bytes"] == 2048


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_finalize_seed_with_copy(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/finalize-seed without skip_copy copies and deletes seed."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"seed-copy-{uuid.uuid4().hex[:8]}",
        state="pending",
        format="qcow2",
    )

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {"ContentLength": 4096}
    mock_client_fn.return_value = mock_s3

    resp = client.post(
        f"/api/v1/library/{item_id}/finalize-seed",
        json={"seed_key": "seeds/original.qcow2", "skip_copy": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    assert data["size_bytes"] == 4096
    # Verify copy + delete were called
    mock_s3.copy_object.assert_called_once()
    mock_s3.delete_object.assert_called_once()


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_finalize_seed_with_tags(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/finalize-seed sets tags from body."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"seed-tags-{uuid.uuid4().hex[:8]}",
        state="pending",
    )

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {"ContentLength": 1024}
    mock_client_fn.return_value = mock_s3

    resp = client.post(
        f"/api/v1/library/{item_id}/finalize-seed",
        json={
            "seed_key": "seeds/tagged.qcow2",
            "skip_copy": True,
            "tags": ["ocp_default_image", "rhel9"],
        },
    )
    assert resp.status_code == 200

    db = TestSession()
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    assert item.tags == {"ocp_default_image": True, "rhel9": True}
    db.close()


def test_finalize_seed_not_found():
    """POST /library/{item_id}/finalize-seed returns 404 for missing item."""
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/library/{fake_id}/finalize-seed",
        json={"seed_key": "seeds/x.qcow2"},
    )
    assert resp.status_code == 404


def test_finalize_seed_access_denied():
    """POST /library/{item_id}/finalize-seed returns 403 for other user's item."""
    _other_uid, other_lib_id = _create_other_user_and_library()
    item_id = _create_library_item(
        other_lib_id, name=f"seed-deny-{uuid.uuid4().hex[:8]}"
    )
    resp = client.post(
        f"/api/v1/library/{item_id}/finalize-seed",
        json={"seed_key": "seeds/x.qcow2"},
    )
    assert resp.status_code == 403


# ===========================================================================
# POST /{item_id}/import-url
# ===========================================================================


@patch("threading.Thread")
def test_import_url_success(mock_thread):
    """POST /library/{item_id}/import-url returns importing state."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"import-url-{uuid.uuid4().hex[:8]}",
        state="pending",
    )

    mock_thread_instance = MagicMock()
    mock_thread.return_value = mock_thread_instance

    resp = client.post(
        f"/api/v1/library/{item_id}/import-url",
        json={"url": "https://example.com/image.qcow2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "importing"
    assert data["id"] == item_id

    # Verify the background thread was started
    mock_thread_instance.start.assert_called_once()


def test_import_url_not_found():
    """POST /library/{item_id}/import-url returns 404 for missing item."""
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/library/{fake_id}/import-url",
        json={"url": "https://example.com/image.qcow2"},
    )
    assert resp.status_code == 404


def test_import_url_access_denied():
    """POST /library/{item_id}/import-url returns 403 for other user's item."""
    _other_uid, other_lib_id = _create_other_user_and_library()
    item_id = _create_library_item(
        other_lib_id, name=f"import-deny-{uuid.uuid4().hex[:8]}"
    )
    resp = client.post(
        f"/api/v1/library/{item_id}/import-url",
        json={"url": "https://example.com/image.qcow2"},
    )
    assert resp.status_code == 403


# ===========================================================================
# POST /{item_id}/cancel
# ===========================================================================


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_cancel_import_success(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/cancel cleans up S3 and deletes item."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"cancel-ok-{uuid.uuid4().hex[:8]}",
        s3_key="library/test/cancel.qcow2",
        state="importing",
    )

    mock_s3 = MagicMock()
    mock_s3.list_multipart_uploads.return_value = {
        "Uploads": [{"Key": "library/test/cancel.qcow2", "UploadId": "upl-cancel"}]
    }
    mock_client_fn.return_value = mock_s3

    resp = client.post(f"/api/v1/library/{item_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Verify item was deleted
    db = TestSession()
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    assert item is None
    db.close()

    # Verify S3 cleanup was called
    mock_s3.abort_multipart_upload.assert_called_once()
    mock_s3.delete_object.assert_called_once()


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_cancel_import_no_s3_key(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/cancel works even without s3_key."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"cancel-nokey-{uuid.uuid4().hex[:8]}",
        s3_key=None,
        state="pending",
    )

    resp = client.post(f"/api/v1/library/{item_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # S3 client should not have been called
    mock_client_fn.assert_not_called()

    # Item should be deleted
    db = TestSession()
    item = db.query(LibraryItem).filter_by(id=item_id).first()
    assert item is None
    db.close()


def test_cancel_import_not_found():
    """POST /library/{item_id}/cancel returns 404 for missing item."""
    fake_id = str(uuid.uuid4())
    resp = client.post(f"/api/v1/library/{fake_id}/cancel")
    assert resp.status_code == 404


def test_cancel_import_access_denied():
    """POST /library/{item_id}/cancel returns 403 for other user's item."""
    _other_uid, other_lib_id = _create_other_user_and_library()
    item_id = _create_library_item(
        other_lib_id,
        name=f"cancel-deny-{uuid.uuid4().hex[:8]}",
        state="importing",
    )
    resp = client.post(f"/api/v1/library/{item_id}/cancel")
    assert resp.status_code == 403


# ===========================================================================
# POST /{item_id}/share
# ===========================================================================


def test_share_item_not_found():
    """POST /library/{item_id}/share returns 404 for missing item."""
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/library/{fake_id}/share",
        json={"user_email": "nobody@test.com"},
    )
    assert resp.status_code == 404


def test_share_item_wrong_owner():
    """POST /library/{item_id}/share returns 403 for other user's item."""
    _other_uid, other_lib_id = _create_other_user_and_library()
    item_id = _create_library_item(
        other_lib_id, name=f"share-deny-{uuid.uuid4().hex[:8]}"
    )
    resp = client.post(
        f"/api/v1/library/{item_id}/share",
        json={"user_email": "someone@test.com"},
    )
    assert resp.status_code == 403


def test_share_item_target_user_not_found():
    """POST /library/{item_id}/share returns 404 when target user does not exist."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"share-nouser-{uuid.uuid4().hex[:8]}")
    resp = client.post(
        f"/api/v1/library/{item_id}/share",
        json={"user_email": f"nonexistent-{uuid.uuid4().hex[:8]}@test.com"},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_share_item_with_self():
    """POST /library/{item_id}/share returns 400 when sharing with self."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"share-self-{uuid.uuid4().hex[:8]}")
    resp = client.post(
        f"/api/v1/library/{item_id}/share",
        json={"user_email": "local-dev@troshka"},
    )
    assert resp.status_code == 400
    assert "Cannot share with yourself" in resp.json()["detail"]


def test_share_item_success():
    """POST /library/{item_id}/share creates a share record."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"share-ok-{uuid.uuid4().hex[:8]}")

    # Create a target user to share with
    other_uid, _other_lib = _create_other_user_and_library()
    db = TestSession()
    other_user = db.query(User).filter_by(id=other_uid).first()
    other_email = other_user.email
    db.close()

    resp = client.post(
        f"/api/v1/library/{item_id}/share",
        json={"user_email": other_email},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["shared_with"] == other_email
    assert data["permission"] == "use"

    # Verify share record exists
    db = TestSession()
    share = (
        db.query(LibraryShare)
        .filter_by(item_id=item_id, shared_with_id=other_uid)
        .first()
    )
    assert share is not None
    assert share.permission == "use"
    db.close()


def test_share_item_update_permission():
    """POST /library/{item_id}/share updates existing share permission."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"share-upd-{uuid.uuid4().hex[:8]}")

    other_uid, _other_lib = _create_other_user_and_library()
    db = TestSession()
    other_user = db.query(User).filter_by(id=other_uid).first()
    other_email = other_user.email
    db.close()

    # First share
    resp = client.post(
        f"/api/v1/library/{item_id}/share",
        json={"user_email": other_email, "permission": "use"},
    )
    assert resp.status_code == 200

    # Update permission
    resp = client.post(
        f"/api/v1/library/{item_id}/share",
        json={"user_email": other_email, "permission": "edit"},
    )
    assert resp.status_code == 200
    assert resp.json()["permission"] == "edit"

    # Verify only one share exists with updated permission
    db = TestSession()
    shares = (
        db.query(LibraryShare)
        .filter_by(item_id=item_id, shared_with_id=other_uid)
        .all()
    )
    assert len(shares) == 1
    assert shares[0].permission == "edit"
    db.close()


# ===========================================================================
# DELETE /{item_id}/share/{user_email}
# ===========================================================================


def test_unshare_item_not_found():
    """DELETE /library/{item_id}/share/{email} returns 404 for missing item."""
    fake_id = str(uuid.uuid4())
    resp = client.delete(f"/api/v1/library/{fake_id}/share/test@test.com")
    assert resp.status_code == 404


def test_unshare_item_wrong_owner():
    """DELETE /library/{item_id}/share/{email} returns 403 for other user's item."""
    _other_uid, other_lib_id = _create_other_user_and_library()
    item_id = _create_library_item(
        other_lib_id, name=f"unshare-deny-{uuid.uuid4().hex[:8]}"
    )
    resp = client.delete(f"/api/v1/library/{item_id}/share/someone@test.com")
    assert resp.status_code == 403


def test_unshare_item_target_not_found():
    """DELETE /library/{item_id}/share/{email} returns 404 for nonexistent target."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id, name=f"unshare-nouser-{uuid.uuid4().hex[:8]}"
    )
    resp = client.delete(
        f"/api/v1/library/{item_id}/share/nonexistent-{uuid.uuid4().hex[:8]}@test.com"
    )
    assert resp.status_code == 404


def test_unshare_item_success():
    """DELETE /library/{item_id}/share/{email} removes the share."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"unshare-ok-{uuid.uuid4().hex[:8]}")

    other_uid, _other_lib = _create_other_user_and_library()
    db = TestSession()
    other_user = db.query(User).filter_by(id=other_uid).first()
    other_email = other_user.email
    # Create a share manually
    db.add(LibraryShare(item_id=item_id, shared_with_id=other_uid, permission="use"))
    db.commit()
    db.close()

    resp = client.delete(f"/api/v1/library/{item_id}/share/{other_email}")
    assert resp.status_code == 200
    assert resp.json()["unshared"] == other_email

    # Verify share was removed
    db = TestSession()
    share = (
        db.query(LibraryShare)
        .filter_by(item_id=item_id, shared_with_id=other_uid)
        .first()
    )
    assert share is None
    db.close()


def test_unshare_item_no_existing_share():
    """DELETE /library/{item_id}/share/{email} succeeds even without existing share."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(lib_id, name=f"unshare-noop-{uuid.uuid4().hex[:8]}")

    other_uid, _other_lib = _create_other_user_and_library()
    db = TestSession()
    other_user = db.query(User).filter_by(id=other_uid).first()
    other_email = other_user.email
    db.close()

    resp = client.delete(f"/api/v1/library/{item_id}/share/{other_email}")
    assert resp.status_code == 200


# ===========================================================================
# POST /sync-central
# ===========================================================================


def test_sync_central_non_admin():
    """POST /library/sync-central returns 403 for non-admin users.

    The dev user is admin, so we patch get_current_user to return a
    non-admin user.
    """
    _user_id = _ensure_dev_user()

    db = TestSession()
    non_admin = User(
        id=str(uuid.uuid4()),
        email=f"nonadmin-{uuid.uuid4().hex[:8]}@test.com",
        display_name="Non Admin",
        role="user",
        auth_source="test",
    )
    db.add(non_admin)
    db.commit()
    non_admin_id = non_admin.id
    db.close()

    from app.core.auth import get_current_user

    def _mock_non_admin_user(request=None, db=None):
        if db is None:
            _db = TestSession()
        else:
            _db = db
        return _db.query(User).filter_by(id=non_admin_id).first()

    app.dependency_overrides[get_current_user] = _mock_non_admin_user
    try:
        resp = client.post("/api/v1/library/sync-central")
        assert resp.status_code == 403
        assert "Admin only" in resp.json()["detail"]
    finally:
        app.dependency_overrides[get_current_user] = None
        del app.dependency_overrides[get_current_user]


@patch("app.services.central_library.sync_central_library")
def test_sync_central_success(mock_sync):
    """POST /library/sync-central calls sync and returns result for admin."""
    _ensure_dev_user()
    mock_sync.return_value = {"synced": 5, "skipped": 2}

    resp = client.post("/api/v1/library/sync-central")
    assert resp.status_code == 200


# ===========================================================================
# POST /scan-s3
# ===========================================================================


def test_scan_s3_non_admin():
    """POST /library/scan-s3 returns 403 for non-admin users."""
    _user_id = _ensure_dev_user()

    db = TestSession()
    non_admin = User(
        id=str(uuid.uuid4()),
        email=f"nonadmin-scan-{uuid.uuid4().hex[:8]}@test.com",
        display_name="Non Admin",
        role="user",
        auth_source="test",
    )
    db.add(non_admin)
    db.commit()
    non_admin_id = non_admin.id
    db.close()

    from app.core.auth import get_current_user

    def _mock_non_admin_user(request=None, db=None):
        if db is None:
            _db = TestSession()
        else:
            _db = db
        return _db.query(User).filter_by(id=non_admin_id).first()

    app.dependency_overrides[get_current_user] = _mock_non_admin_user
    try:
        resp = client.post("/api/v1/library/scan-s3")
        assert resp.status_code == 403
        assert "Admin only" in resp.json()["detail"]
    finally:
        app.dependency_overrides[get_current_user] = None
        del app.dependency_overrides[get_current_user]


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_scan_s3_success_empty(mock_client_fn, mock_bucket):
    """POST /library/scan-s3 returns counts when S3 has no objects."""
    _ensure_dev_user()

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {"Contents": [], "IsTruncated": False}
    mock_client_fn.return_value = mock_s3

    resp = client.post("/api/v1/library/scan-s3")
    assert resp.status_code == 200
    data = resp.json()
    assert "imported" in data
    assert "snapshots" in data
    assert "patterns" in data
    assert data["bucket"] == "test-bucket"


# ===========================================================================
# POST /{item_id}/upload-proxy
# ===========================================================================


@patch("app.api.library.s3_storage._bucket", return_value="test-bucket")
@patch("app.api.library.s3_storage._get_s3_client")
def test_upload_proxy_success(mock_client_fn, mock_bucket):
    """POST /library/{item_id}/upload-proxy streams file to S3."""
    _user_id, lib_id = _ensure_user_and_library()
    item_id = _create_library_item(
        lib_id,
        name=f"proxy-upl-{uuid.uuid4().hex[:8]}",
        state="pending",
    )

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {"ContentLength": 512}
    mock_client_fn.return_value = mock_s3

    import io

    file_content = b"fake image data"
    resp = client.post(
        f"/api/v1/library/{item_id}/upload-proxy",
        files={
            "file": ("test.qcow2", io.BytesIO(file_content), "application/octet-stream")
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "s3_key" in data
    assert data["size_bytes"] == 512
    mock_s3.upload_fileobj.assert_called_once()


def test_upload_proxy_not_found():
    """POST /library/{item_id}/upload-proxy returns 404 for missing item."""
    import io

    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/library/{fake_id}/upload-proxy",
        files={"file": ("test.qcow2", io.BytesIO(b"data"), "application/octet-stream")},
    )
    assert resp.status_code == 404


def test_upload_proxy_access_denied():
    """POST /library/{item_id}/upload-proxy returns 403 for other user's item."""
    import io

    _other_uid, other_lib_id = _create_other_user_and_library()
    item_id = _create_library_item(
        other_lib_id, name=f"proxy-deny-{uuid.uuid4().hex[:8]}"
    )
    resp = client.post(
        f"/api/v1/library/{item_id}/upload-proxy",
        files={"file": ("test.qcow2", io.BytesIO(b"data"), "application/octet-stream")},
    )
    assert resp.status_code == 403


# ===========================================================================
# Helper function unit tests
# ===========================================================================


def test_classify_s3_object_simple_file():
    """_classify_s3_object groups a file under its parent ID."""
    from app.api.library import _classify_s3_object

    groups = {}
    mock_client = MagicMock()
    obj = {"Key": "library/user-id/item-id/image.qcow2", "Size": 1024}
    _classify_s3_object(mock_client, "bucket", groups, obj)

    assert "user-id" in groups
    assert len(groups["user-id"]["files"]) == 1
    assert groups["user-id"]["files"][0]["key"] == "library/user-id/item-id/image.qcow2"
    assert groups["user-id"]["files"][0]["size"] == 1024


def test_classify_s3_object_metadata():
    """_classify_s3_object reads metadata.json into group metadata."""
    import json

    from app.api.library import _classify_s3_object

    groups = {}
    mock_client = MagicMock()
    meta_body = MagicMock()
    meta_body.read.return_value = json.dumps({"name": "test-snapshot"}).encode()
    mock_client.get_object.return_value = {"Body": meta_body}

    obj = {"Key": "snapshots/snap-id/metadata.json", "Size": 100}
    _classify_s3_object(mock_client, "bucket", groups, obj)

    assert "snap-id" in groups
    assert groups["snap-id"]["metadata"] == {"name": "test-snapshot"}


def test_classify_s3_object_short_key():
    """_classify_s3_object skips keys with fewer than 3 parts."""
    from app.api.library import _classify_s3_object

    groups = {}
    mock_client = MagicMock()
    obj = {"Key": "library/orphan", "Size": 0}
    _classify_s3_object(mock_client, "bucket", groups, obj)

    assert len(groups) == 0


def test_scan_s3_prefix_paginated():
    """_scan_s3_prefix handles paginated results."""
    from app.api.library import _scan_s3_prefix

    mock_client = MagicMock()
    # First page with truncation
    mock_client.list_objects_v2.side_effect = [
        {
            "Contents": [
                {"Key": "prefix/id1/file1.qcow2", "Size": 100},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "token123",
        },
        {
            "Contents": [
                {"Key": "prefix/id2/file2.qcow2", "Size": 200},
            ],
            "IsTruncated": False,
        },
    ]

    groups = _scan_s3_prefix(mock_client, "bucket", "prefix/")
    assert "id1" in groups
    assert "id2" in groups
    assert mock_client.list_objects_v2.call_count == 2


def test_try_import_library_object_new():
    """_try_import_library_object creates a new LibraryItem from S3 key."""
    from app.api.library import _try_import_library_object

    _user_id, lib_id = _ensure_user_and_library()
    db = TestSession()
    lib = db.query(Library).filter_by(id=lib_id).first()

    fake_item_id = str(uuid.uuid4())
    obj = {
        "Key": f"library/user/{fake_item_id}/rhel9.qcow2",
        "Size": 10240,
    }

    result = _try_import_library_object(db, lib, obj)
    assert result is True

    item = db.query(LibraryItem).filter_by(id=fake_item_id).first()
    assert item is not None
    assert item.name == "rhel9"
    assert item.format == "qcow2"
    assert item.type == "image"
    assert item.size_bytes == 10240
    assert item.state == "ready"

    # Clean up
    db.delete(item)
    db.commit()
    db.close()


def test_try_import_library_object_iso_type():
    """_try_import_library_object sets type=iso for .iso files."""
    from app.api.library import _try_import_library_object

    _user_id, lib_id = _ensure_user_and_library()
    db = TestSession()
    lib = db.query(Library).filter_by(id=lib_id).first()

    fake_item_id = str(uuid.uuid4())
    obj = {
        "Key": f"library/user/{fake_item_id}/rhel9-boot.iso",
        "Size": 819200,
    }

    result = _try_import_library_object(db, lib, obj)
    assert result is True

    item = db.query(LibraryItem).filter_by(id=fake_item_id).first()
    assert item.type == "iso"
    assert item.format == "iso"

    db.delete(item)
    db.commit()
    db.close()


def test_try_import_library_object_already_exists():
    """_try_import_library_object returns False if item ID already in DB."""
    from app.api.library import _try_import_library_object

    _user_id, lib_id = _ensure_user_and_library()
    existing_id = _create_library_item(lib_id, name=f"existing-{uuid.uuid4().hex[:8]}")

    db = TestSession()
    lib = db.query(Library).filter_by(id=lib_id).first()
    obj = {
        "Key": f"library/user/{existing_id}/dup.qcow2",
        "Size": 100,
    }

    result = _try_import_library_object(db, lib, obj)
    assert result is False
    db.close()


def test_try_import_library_object_short_key():
    """_try_import_library_object returns False for keys with < 4 parts."""
    from app.api.library import _try_import_library_object

    _user_id, lib_id = _ensure_user_and_library()
    db = TestSession()
    lib = db.query(Library).filter_by(id=lib_id).first()

    obj = {"Key": "library/user/nofile", "Size": 0}
    result = _try_import_library_object(db, lib, obj)
    assert result is False
    db.close()


def test_try_import_library_object_no_extension():
    """_try_import_library_object handles files without extension."""
    from app.api.library import _try_import_library_object

    _user_id, lib_id = _ensure_user_and_library()
    db = TestSession()
    lib = db.query(Library).filter_by(id=lib_id).first()

    fake_item_id = str(uuid.uuid4())
    obj = {
        "Key": f"library/user/{fake_item_id}/noext",
        "Size": 512,
    }

    result = _try_import_library_object(db, lib, obj)
    assert result is True

    item = db.query(LibraryItem).filter_by(id=fake_item_id).first()
    assert item.name == "noext"
    assert item.format == "unknown"
    assert item.type == "image"

    db.delete(item)
    db.commit()
    db.close()


@patch("app.services.troshkad_client.check_disk_usage", return_value={"used_pct": 50})
def test_find_import_host_no_hosts(mock_disk):
    """_find_import_host returns None when no active connected hosts exist."""
    from app.api.library import _find_import_host

    db = TestSession()
    _result = _find_import_host(db, "test-item-id")
    # If there are no active connected hosts, should be None
    # The mock prevents the troshkad_client from trying real connections
    db.close()


@patch("app.services.troshkad_client.check_disk_usage")
def test_find_import_host_disk_full(mock_disk):
    """_find_import_host returns None when host disk is >= 90% full."""
    from app.api.library import _find_import_host
    from app.models.host import Host

    db = TestSession()

    # Create a temporary active host
    host = Host(
        id=str(uuid.uuid4()),
        instance_id=f"i-{uuid.uuid4().hex[:8]}",
        instance_type="m5.xlarge",
        region="us-east-1",
        state="active",
        agent_status="connected",
        ip_address="10.0.0.1",
        total_vcpus=4,
        total_ram_mb=16384,
        storage_size_gb=100,
        max_eips=5,
    )
    db.add(host)
    db.commit()
    host_id = host.id

    mock_disk.return_value = {"used_pct": 95, "free_bytes": 5 * 1024**3}

    result = _find_import_host(db, "test-item-id")
    # The host exists but disk is full
    assert result is None

    # Clean up
    db.query(Host).filter_by(id=host_id).delete()
    db.commit()
    db.close()
