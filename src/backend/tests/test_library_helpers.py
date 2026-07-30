"""Tests for library API helper functions."""
import os

os.environ.setdefault("TROSHKA_DATABASE__URL", "sqlite:///./test.db")

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

# ── _check_not_central tests ──


class TestCheckNotCentral:
    def test_raises_for_central_item(self):
        from app.api.library import _check_not_central

        item = MagicMock()
        item.source = "central"
        with pytest.raises(HTTPException) as exc:
            _check_not_central(item)
        assert exc.value.status_code == 403

    def test_passes_for_local_item(self):
        from app.api.library import _check_not_central

        item = MagicMock()
        item.source = "local"
        # Should not raise
        _check_not_central(item)

    def test_passes_for_no_source(self):
        from app.api.library import _check_not_central

        item = MagicMock(spec=[])  # no 'source' attribute
        # Should not raise — getattr returns "local" default
        _check_not_central(item)


# ── _ensure_user_library tests ──


class TestEnsureUserLibrary:
    def test_returns_existing_library(self):
        from app.api.library import _ensure_user_library

        user = MagicMock()
        user.id = "user-1"
        db = MagicMock()
        existing_lib = MagicMock()
        existing_lib.id = "lib-1"
        db.query.return_value.filter_by.return_value.first.return_value = existing_lib

        result = _ensure_user_library(user, db)
        assert result.id == "lib-1"
        db.add.assert_not_called()

    def test_creates_new_library(self):
        from app.api.library import _ensure_user_library

        user = MagicMock()
        user.id = "user-2"
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        _ensure_user_library(user, db)
        db.add.assert_called_once()
        db.commit.assert_called_once()
        db.refresh.assert_called_once()


# ── _do_start_upload tests ──


class TestDoStartUpload:
    def test_creates_multipart_upload(self):
        from app.api.library import _do_start_upload

        mock_client = MagicMock()
        mock_client.create_multipart_upload.return_value = {"UploadId": "upload-123"}

        result = _do_start_upload(
            mock_client, "test-bucket", "library/item.qcow2", "item-id"
        )
        assert result["upload_id"] == "upload-123"
        assert result["s3_key"] == "library/item.qcow2"
        mock_client.create_multipart_upload.assert_called_once_with(
            Bucket="test-bucket",
            Key="library/item.qcow2",
            ContentType="application/octet-stream",
        )

    def test_upload_error_raises(self):
        from app.api.library import _do_start_upload

        mock_client = MagicMock()
        mock_client.create_multipart_upload.side_effect = Exception("S3 error")

        with pytest.raises(Exception, match="S3 error"):
            _do_start_upload(
                mock_client, "test-bucket", "library/item.qcow2", "item-id"
            )


# ── _find_import_host tests ──


class TestFindImportHost:
    def test_returns_host_when_disk_ok(self):
        from app.api.library import _find_import_host

        mock_host = MagicMock()
        mock_host.state = "active"
        mock_host.agent_status = "connected"

        mock_sess = MagicMock()
        mock_sess.query.return_value.filter_by.return_value.first.return_value = (
            mock_host
        )

        with patch(
            "app.services.troshkad_client.check_disk_usage",
            return_value={"used_pct": 50, "free_bytes": 100 * 1024**3},
        ):
            result = _find_import_host(mock_sess, "item1234")
            assert result is mock_host

    def test_returns_none_when_no_active_host(self):
        from app.api.library import _find_import_host

        mock_sess = MagicMock()
        mock_sess.query.return_value.filter_by.return_value.first.return_value = None

        result = _find_import_host(mock_sess, "item1234")
        assert result is None

    def test_returns_none_when_disk_full(self):
        from app.api.library import _find_import_host

        mock_host = MagicMock()
        mock_sess = MagicMock()
        mock_sess.query.return_value.filter_by.return_value.first.return_value = (
            mock_host
        )

        with patch(
            "app.services.troshkad_client.check_disk_usage",
            return_value={"used_pct": 95, "free_bytes": 1 * 1024**3},
        ):
            result = _find_import_host(mock_sess, "item1234")
            assert result is None

    def test_returns_none_when_disk_check_returns_empty(self):
        from app.api.library import _find_import_host

        mock_host = MagicMock()
        mock_sess = MagicMock()
        mock_sess.query.return_value.filter_by.return_value.first.return_value = (
            mock_host
        )

        with patch("app.services.troshkad_client.check_disk_usage", return_value=None):
            # used_pct defaults to 100 when disk check returns None/{}
            result = _find_import_host(mock_sess, "item1234")
            assert result is None

    def test_returns_host_when_disk_at_89_percent(self):
        from app.api.library import _find_import_host

        mock_host = MagicMock()
        mock_sess = MagicMock()
        mock_sess.query.return_value.filter_by.return_value.first.return_value = (
            mock_host
        )

        with patch(
            "app.services.troshkad_client.check_disk_usage",
            return_value={"used_pct": 89, "free_bytes": 50 * 1024**3},
        ):
            result = _find_import_host(mock_sess, "item1234")
            assert result is mock_host


# ── _run_import_job tests ──


class TestRunImportJob:
    def test_successful_import_sets_ready(self):
        from app.api.library import _run_import_job

        mock_host = MagicMock()
        mock_item = MagicMock()
        mock_sess = MagicMock()

        with patch("app.services.s3_storage._get_s3_client") as mock_s3, patch(
            "app.services.s3_storage._bucket", return_value="test-bucket"
        ), patch("app.services.s3_storage._get_s3_config", return_value={}), patch(
            "app.services.troshkad_client.start_job", return_value="job-1"
        ), patch(
            "app.services.troshkad_client.wait_for_job",
            return_value={"status": "completed"},
        ):
            s3 = mock_s3.return_value
            s3.head_object.return_value = {"ContentLength": 5000}

            _run_import_job(
                mock_host, mock_item, mock_sess, "item-1234-abcd", "s3/key", "http://x"
            )

            assert mock_item.state == "ready"
            assert mock_item.size_bytes == 5000
            mock_sess.commit.assert_called()

    def test_failed_job_sets_error(self):
        from app.api.library import _run_import_job

        mock_host = MagicMock()
        mock_item = MagicMock()
        mock_sess = MagicMock()

        with patch(
            "app.services.s3_storage._bucket", return_value="test-bucket"
        ), patch("app.services.s3_storage._get_s3_config", return_value={}), patch(
            "app.services.troshkad_client.start_job", return_value="job-1"
        ), patch(
            "app.services.troshkad_client.wait_for_job",
            return_value={"status": "failed", "error": "disk full"},
        ):
            _run_import_job(
                mock_host, mock_item, mock_sess, "item-1234-abcd", "s3/key", "http://x"
            )

            assert mock_item.state == "error"
            mock_sess.commit.assert_called()

    def test_troshkad_error_sets_error(self):
        from app.api.library import _run_import_job
        from app.services.troshkad_client import TroshkadError

        mock_host = MagicMock()
        mock_item = MagicMock()
        mock_sess = MagicMock()

        with patch(
            "app.services.s3_storage._bucket", return_value="test-bucket"
        ), patch("app.services.s3_storage._get_s3_config", return_value={}), patch(
            "app.services.troshkad_client.start_job",
            side_effect=TroshkadError("connection refused"),
        ):
            _run_import_job(
                mock_host, mock_item, mock_sess, "item-1234-abcd", "s3/key", "http://x"
            )

            assert mock_item.state == "error"
            mock_sess.commit.assert_called()


# ── Pydantic model validation tests ──


class TestLibraryItemCreate:
    def test_defaults(self):
        from app.api.library import LibraryItemCreate

        item = LibraryItemCreate(name="test-image")
        assert item.name == "test-image"
        assert item.description == ""
        assert item.type == "image"
        assert item.format == "qcow2"
        assert item.os_variant == ""
        assert item.tags is None

    def test_all_fields(self):
        from app.api.library import LibraryItemCreate

        item = LibraryItemCreate(
            name="rhel9",
            description="RHEL 9 base",
            type="iso",
            format="iso",
            os_variant="rhel9.0",
            tags=["base", "rhel"],
        )
        assert item.name == "rhel9"
        assert item.type == "iso"
        assert item.format == "iso"
        assert item.os_variant == "rhel9.0"
        assert item.tags == ["base", "rhel"]

    def test_missing_name_raises(self):
        from app.api.library import LibraryItemCreate

        with pytest.raises(Exception):
            LibraryItemCreate()


class TestLibraryItemUpdate:
    def test_all_none_defaults(self):
        from app.api.library import LibraryItemUpdate

        body = LibraryItemUpdate()
        assert body.name is None
        assert body.description is None
        assert body.source_url is None
        assert body.tags is None

    def test_partial_update(self):
        from app.api.library import LibraryItemUpdate

        body = LibraryItemUpdate(name="new-name", tags={"ocp_default_image": True})
        assert body.name == "new-name"
        assert body.description is None
        assert body.tags == {"ocp_default_image": True}


class TestLibraryItemResponse:
    def test_from_dict(self):
        from app.api.library import LibraryItemResponse

        resp = LibraryItemResponse(
            id="item-1",
            library_id="lib-1",
            name="test",
            type="image",
            format="qcow2",
            size_bytes=1024,
            state="ready",
            created_at="2025-01-01",
        )
        assert resp.id == "item-1"
        assert resp.size_bytes == 1024
        assert resp.s3_key is None
        assert resp.checksum_sha256 is None
        assert resp.tags is None


class TestDoStartUploadItemId:
    """Additional edge-case tests for _do_start_upload."""

    def test_returns_correct_keys(self):
        from app.api.library import _do_start_upload

        mock_client = MagicMock()
        mock_client.create_multipart_upload.return_value = {"UploadId": "up-999"}

        result = _do_start_upload(
            mock_client, "my-bucket", "library/x/y.qcow2", "item-999"
        )
        assert "upload_id" in result
        assert "s3_key" in result
        assert result["upload_id"] == "up-999"
        assert result["s3_key"] == "library/x/y.qcow2"

    def test_content_type_is_octet_stream(self):
        from app.api.library import _do_start_upload

        mock_client = MagicMock()
        mock_client.create_multipart_upload.return_value = {"UploadId": "up-1"}

        _do_start_upload(mock_client, "bucket", "key", "id")
        call_kwargs = mock_client.create_multipart_upload.call_args[1]
        assert call_kwargs["ContentType"] == "application/octet-stream"
