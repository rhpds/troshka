"""Tests for app.services.s3_storage — covers all public and private functions."""

import os

os.environ.setdefault("TROSHKA_DATABASE__URL", "sqlite:///./test.db")

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_config(**overrides):
    """Return a minimal S3 config dict, with optional overrides."""
    cfg = {
        "region": "us-east-1",
        "access_key_id": "test-access-key-id",  # pragma: allowlist secret
        "secret_access_key": "test-secret-key",  # pragma: allowlist secret
        "bucket": "test-bucket",
        "endpoint_url": "",
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# _get_s3_config
# ---------------------------------------------------------------------------


class TestGetS3Config:
    """_get_s3_config returns config from DB provider or config.yaml fallback."""

    @patch("app.core.database.SessionLocal")
    def test_returns_config_from_db_provider(self, mock_sl):
        mock_provider = MagicMock()
        mock_provider.default_region = "eu-west-1"
        mock_provider.get_credentials.return_value = {
            "region": "eu-west-1",
            "access_key_id": "AKIA_DB",
            "secret_access_key": "secret_db",
            "bucket": "db-bucket",
            "endpoint_url": "http://minio:9000",
        }
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_provider
        )
        mock_sl.return_value = mock_session

        from app.services.s3_storage import _get_s3_config

        cfg = _get_s3_config()
        assert cfg["access_key_id"] == "AKIA_DB"
        assert cfg["bucket"] == "db-bucket"
        assert cfg["endpoint_url"] == "http://minio:9000"
        assert cfg["region"] == "eu-west-1"
        mock_session.close.assert_called()

    @patch("app.core.database.SessionLocal")
    def test_db_provider_no_endpoint_url(self, mock_sl):
        """When provider creds have no endpoint_url, defaults to empty string."""
        mock_provider = MagicMock()
        mock_provider.default_region = "us-west-2"
        mock_provider.get_credentials.return_value = {
            "region": "",
            "access_key_id": "AKIA_X",
            "secret_access_key": "sec_x",
            "bucket": "b",
        }
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_provider
        )
        mock_sl.return_value = mock_session

        from app.services.s3_storage import _get_s3_config

        cfg = _get_s3_config()
        assert cfg["endpoint_url"] == ""
        # region falls back to provider.default_region when creds.region is empty
        assert cfg["region"] == "us-west-2"

    @patch("app.core.database.SessionLocal")
    def test_falls_back_to_config_yaml(self, mock_sl):
        """When DB has no S3 provider, falls back to config.yaml."""
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None
        mock_sl.return_value = mock_session

        mock_config = MagicMock()
        mock_config.s3.region = "ap-south-1"
        mock_config.s3.access_key_id = "AKIA_CFG"
        mock_config.s3.secret_access_key = "sec_cfg"
        mock_config.s3.bucket = "cfg-bucket"
        mock_config.s3.endpoint_url = ""

        with patch("app.services.s3_storage.config", mock_config):
            from app.services.s3_storage import _get_s3_config

            cfg = _get_s3_config()
            assert cfg["access_key_id"] == "AKIA_CFG"
            assert cfg["bucket"] == "cfg-bucket"
            assert cfg["region"] == "ap-south-1"

    @patch("app.core.database.SessionLocal")
    def test_raises_when_both_db_and_config_fail(self, mock_sl):
        """Raises ValueError when neither DB nor config.yaml has S3 settings."""
        mock_sl.side_effect = Exception("db unavailable")

        mock_config = MagicMock()
        mock_config.s3 = MagicMock(
            spec=[],  # empty spec -> any attribute access raises AttributeError
        )
        type(mock_config.s3).region = property(
            lambda self: (_ for _ in ()).throw(AttributeError("no region"))
        )

        with patch("app.services.s3_storage.config", mock_config):
            from app.services.s3_storage import _get_s3_config

            with pytest.raises(ValueError, match="No S3 provider configured"):
                _get_s3_config()

    @patch("app.core.database.SessionLocal")
    def test_db_exception_falls_through(self, mock_sl):
        """DB exception is swallowed and config.yaml is tried."""
        mock_sl.side_effect = Exception("connection refused")

        mock_config = MagicMock()
        mock_config.s3.region = "us-east-1"
        mock_config.s3.access_key_id = "AKIA_FB"
        mock_config.s3.secret_access_key = "sec_fb"
        mock_config.s3.bucket = "fallback-bucket"
        mock_config.s3.endpoint_url = ""

        with patch("app.services.s3_storage.config", mock_config):
            from app.services.s3_storage import _get_s3_config

            cfg = _get_s3_config()
            assert cfg["bucket"] == "fallback-bucket"


# ---------------------------------------------------------------------------
# _get_s3_client
# ---------------------------------------------------------------------------


class TestGetS3Client:
    """_get_s3_client creates boto3 client with correct kwargs."""

    @patch("app.services.s3_storage.boto3")
    @patch("app.services.s3_storage._get_s3_config")
    def test_with_all_credentials(self, mock_cfg, mock_boto):
        mock_cfg.return_value = _base_config(endpoint_url="http://minio:9000")

        from app.services.s3_storage import _get_s3_client

        _get_s3_client()
        mock_boto.client.assert_called_once_with(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test-access-key-id",  # pragma: allowlist secret
            aws_secret_access_key="test-secret-key",  # pragma: allowlist secret
            endpoint_url="http://minio:9000",
        )

    @patch("app.services.s3_storage.boto3")
    @patch("app.services.s3_storage._get_s3_config")
    def test_without_access_key(self, mock_cfg, mock_boto):
        """When access_key_id is empty, it is not passed to boto3."""
        mock_cfg.return_value = _base_config(access_key_id="", secret_access_key="")

        from app.services.s3_storage import _get_s3_client

        _get_s3_client()
        call_kwargs = mock_boto.client.call_args[1]
        assert "aws_access_key_id" not in call_kwargs
        assert "aws_secret_access_key" not in call_kwargs

    @patch("app.services.s3_storage.boto3")
    @patch("app.services.s3_storage._get_s3_config")
    def test_without_endpoint_url(self, mock_cfg, mock_boto):
        """When endpoint_url is empty, it is not passed to boto3."""
        mock_cfg.return_value = _base_config(endpoint_url="")

        from app.services.s3_storage import _get_s3_client

        _get_s3_client()
        call_kwargs = mock_boto.client.call_args[1]
        assert "endpoint_url" not in call_kwargs


# ---------------------------------------------------------------------------
# _bucket
# ---------------------------------------------------------------------------


class TestBucket:
    @patch("app.services.s3_storage._get_s3_config")
    def test_returns_bucket_name(self, mock_cfg):
        mock_cfg.return_value = _base_config(bucket="my-images")

        from app.services.s3_storage import _bucket

        assert _bucket() == "my-images"


# ---------------------------------------------------------------------------
# ensure_dev_bucket_exists
# ---------------------------------------------------------------------------


class TestEnsureDevBucketExists:
    @patch("app.services.s3_storage._get_s3_config")
    def test_noop_when_no_endpoint_url(self, mock_cfg):
        """Real AWS S3 (no endpoint_url) is left untouched — bucket must already exist."""
        mock_cfg.return_value = _base_config(endpoint_url="")

        from app.services.s3_storage import ensure_dev_bucket_exists

        ensure_dev_bucket_exists()  # should not raise, and calls no boto3 client

    @patch("app.services.s3_storage._get_s3_config")
    def test_noop_when_s3_not_configured(self, mock_cfg):
        """When _get_s3_config raises (no provider/config at all), silently no-ops."""
        mock_cfg.side_effect = ValueError("No S3 provider configured")

        from app.services.s3_storage import ensure_dev_bucket_exists

        ensure_dev_bucket_exists()  # should not raise

    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_skips_create_when_bucket_already_exists(self, mock_cfg, mock_client_fn):
        mock_cfg.return_value = _base_config(endpoint_url="http://minio:9000")
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client

        from app.services.s3_storage import ensure_dev_bucket_exists

        ensure_dev_bucket_exists()
        mock_client.head_bucket.assert_called_once_with(Bucket="test-bucket")
        mock_client.create_bucket.assert_not_called()

    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_creates_bucket_when_missing(self, mock_cfg, mock_client_fn):
        mock_cfg.return_value = _base_config(endpoint_url="http://minio:9000")
        mock_client = MagicMock()
        mock_client.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
        )
        mock_client_fn.return_value = mock_client

        from app.services.s3_storage import ensure_dev_bucket_exists

        ensure_dev_bucket_exists()
        mock_client.create_bucket.assert_called_once_with(Bucket="test-bucket")

    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_swallows_create_bucket_errors(self, mock_cfg, mock_client_fn):
        """Errors creating the bucket (e.g. MinIO unreachable) are logged, not raised."""
        mock_cfg.return_value = _base_config(endpoint_url="http://minio:9000")
        mock_client = MagicMock()
        mock_client.head_bucket.side_effect = Exception("connection refused")
        mock_client.create_bucket.side_effect = Exception("still unreachable")
        mock_client_fn.return_value = mock_client

        from app.services.s3_storage import ensure_dev_bucket_exists

        ensure_dev_bucket_exists()  # should not raise


# ---------------------------------------------------------------------------
# _get_account_id
# ---------------------------------------------------------------------------


class TestGetAccountId:
    def setup_method(self):
        import app.services.s3_storage as mod

        self._mod = mod
        self._orig = mod._cached_account_ids.copy()
        mod._cached_account_ids.clear()

    def teardown_method(self):
        self._mod._cached_account_ids.clear()
        self._mod._cached_account_ids.update(self._orig)

    def test_returns_empty_for_custom_endpoint(self):
        """Non-AWS endpoints (S4/MinIO) skip STS entirely."""
        from app.services.s3_storage import _get_account_id

        result = _get_account_id({"endpoint_url": "http://minio:9000"})
        assert result == ""

    @patch("app.services.s3_storage.boto3")
    def test_returns_cached_value(self, mock_boto):
        import app.services.s3_storage as mod
        from app.services.s3_storage import _get_account_id

        mod._cached_account_ids["AKIA_CACHED"] = "123456789012"
        result = _get_account_id(
            {
                "access_key_id": "AKIA_CACHED",
                "endpoint_url": "",
            }
        )
        assert result == "123456789012"
        mock_boto.client.assert_not_called()

    @patch("app.services.s3_storage.boto3")
    def test_sts_success(self, mock_boto):
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "111222333444"}
        mock_boto.client.return_value = mock_sts

        from app.services.s3_storage import _get_account_id

        result = _get_account_id(_base_config(endpoint_url=""))
        assert result == "111222333444"
        # Verify it was cached
        import app.services.s3_storage as mod

        assert (
            mod._cached_account_ids["test-access-key-id"] == "111222333444"
        )  # pragma: allowlist secret

    @patch("app.services.s3_storage.boto3")
    def test_sts_failure_returns_empty(self, mock_boto):
        mock_boto.client.side_effect = Exception("STS unavailable")

        from app.services.s3_storage import _get_account_id

        result = _get_account_id(_base_config(endpoint_url=""))
        assert result == ""

    @patch("app.services.s3_storage.boto3")
    def test_default_cache_key_when_no_access_key(self, mock_boto):
        """Uses '_default' cache key when access_key_id is missing."""
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "999888777666"}
        mock_boto.client.return_value = mock_sts

        from app.services.s3_storage import _get_account_id

        result = _get_account_id({"endpoint_url": ""})
        assert result == "999888777666"
        import app.services.s3_storage as mod

        assert "_default" in mod._cached_account_ids


# ---------------------------------------------------------------------------
# owner_params
# ---------------------------------------------------------------------------


class TestOwnerParams:
    @patch("app.services.s3_storage._get_account_id")
    def test_with_account_id(self, mock_get_id):
        mock_get_id.return_value = "123456789012"

        from app.services.s3_storage import owner_params

        result = owner_params({"some": "config"})
        assert result == {"ExpectedBucketOwner": "123456789012"}

    @patch("app.services.s3_storage._get_account_id")
    def test_without_account_id(self, mock_get_id):
        mock_get_id.return_value = ""

        from app.services.s3_storage import owner_params

        result = owner_params({"some": "config"})
        assert result == {}


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------


class TestUploadFile:
    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_uploads_and_returns_key_and_size(self, mock_cfg, mock_client_fn, mock_op):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client.head_object.return_value = {"ContentLength": 4096}
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {}

        from app.services.s3_storage import upload_file

        file_obj = MagicMock()
        result = upload_file("library/abc.qcow2", file_obj, "application/x-qemu-disk")

        assert result == {"key": "library/abc.qcow2", "size_bytes": 4096}
        mock_client.upload_fileobj.assert_called_once_with(
            file_obj,
            "test-bucket",
            "library/abc.qcow2",
            ExtraArgs={"ContentType": "application/x-qemu-disk"},
        )
        mock_client.head_object.assert_called_once_with(
            Bucket="test-bucket", Key="library/abc.qcow2"
        )

    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_upload_with_owner_params(self, mock_cfg, mock_client_fn, mock_op):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client.head_object.return_value = {"ContentLength": 1024}
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {"ExpectedBucketOwner": "111222333444"}

        from app.services.s3_storage import upload_file

        _result = upload_file("key.iso", MagicMock())
        # owner_params should be merged into ExtraArgs
        call_kwargs = mock_client.upload_fileobj.call_args
        assert call_kwargs[1]["ExtraArgs"]["ExpectedBucketOwner"] == "111222333444"
        assert call_kwargs[1]["ExtraArgs"]["ContentType"] == "application/octet-stream"
        # head_object also gets owner params
        head_kwargs = mock_client.head_object.call_args[1]
        assert head_kwargs["ExpectedBucketOwner"] == "111222333444"


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------


class TestDownloadFile:
    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_downloads_to_local_path(self, mock_cfg, mock_client_fn, mock_op):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {}

        from app.services.s3_storage import download_file

        download_file("patterns/p1/disk.qcow2", "/tmp/disk.qcow2")
        mock_client.download_file.assert_called_once_with(
            "test-bucket",
            "patterns/p1/disk.qcow2",
            "/tmp/disk.qcow2",
            ExtraArgs=None,
        )

    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_download_with_owner_params(self, mock_cfg, mock_client_fn, mock_op):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {"ExpectedBucketOwner": "111222333444"}

        from app.services.s3_storage import download_file

        download_file("key.iso", "/tmp/key.iso")
        mock_client.download_file.assert_called_once_with(
            "test-bucket",
            "key.iso",
            "/tmp/key.iso",
            ExtraArgs={"ExpectedBucketOwner": "111222333444"},
        )


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


class TestDeleteFile:
    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_deletes_by_key(self, mock_cfg, mock_client_fn, mock_op):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {}

        from app.services.s3_storage import delete_file

        delete_file("library/old.iso")
        mock_client.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key="library/old.iso"
        )


# ---------------------------------------------------------------------------
# delete_prefix
# ---------------------------------------------------------------------------


class TestDeletePrefix:
    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_deletes_objects_under_prefix(self, mock_cfg, mock_client_fn, mock_op):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {}

        # Simulate paginator returning one page with 2 objects
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "patterns/p1/a.qcow2"},
                    {"Key": "patterns/p1/b.qcow2"},
                ]
            },
        ]
        mock_client.get_paginator.return_value = mock_paginator

        from app.services.s3_storage import delete_prefix

        delete_prefix("patterns/p1/")
        mock_client.delete_objects.assert_called_once_with(
            Bucket="test-bucket",
            Delete={
                "Objects": [
                    {"Key": "patterns/p1/a.qcow2"},
                    {"Key": "patterns/p1/b.qcow2"},
                ]
            },
        )

    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_no_objects_under_prefix(self, mock_cfg, mock_client_fn, mock_op):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {}

        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"Contents": []},
        ]
        mock_client.get_paginator.return_value = mock_paginator

        from app.services.s3_storage import delete_prefix

        delete_prefix("empty-prefix/")
        mock_client.delete_objects.assert_not_called()

    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_page_without_contents_key(self, mock_cfg, mock_client_fn, mock_op):
        """Paginator may return pages with no 'Contents' key at all."""
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {}

        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{}]  # no "Contents" key
        mock_client.get_paginator.return_value = mock_paginator

        from app.services.s3_storage import delete_prefix

        delete_prefix("missing/")
        mock_client.delete_objects.assert_not_called()

    @patch("app.services.s3_storage.owner_params")
    @patch("app.services.s3_storage._get_s3_client")
    @patch("app.services.s3_storage._get_s3_config")
    def test_multiple_pages(self, mock_cfg, mock_client_fn, mock_op):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_op.return_value = {}

        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": "p/a"}]},
            {"Contents": [{"Key": "p/b"}, {"Key": "p/c"}]},
        ]
        mock_client.get_paginator.return_value = mock_paginator

        from app.services.s3_storage import delete_prefix

        delete_prefix("p/")
        assert mock_client.delete_objects.call_count == 2


# ---------------------------------------------------------------------------
# generate_presigned_url
# ---------------------------------------------------------------------------


class TestGeneratePresignedUrl:
    @patch("app.services.s3_storage._bucket")
    @patch("app.services.s3_storage._get_s3_client")
    def test_returns_url(self, mock_client_fn, mock_bucket):
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = (
            "https://s3.example.com/signed"
        )
        mock_client_fn.return_value = mock_client
        mock_bucket.return_value = "test-bucket"

        from app.services.s3_storage import generate_presigned_url

        url = generate_presigned_url("library/img.qcow2", expires=7200)
        assert url == "https://s3.example.com/signed"
        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "test-bucket", "Key": "library/img.qcow2"},
            ExpiresIn=7200,
        )

    @patch("app.services.s3_storage._bucket")
    @patch("app.services.s3_storage._get_s3_client")
    def test_default_expiry(self, mock_client_fn, mock_bucket):
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://url"
        mock_client_fn.return_value = mock_client
        mock_bucket.return_value = "b"

        from app.services.s3_storage import generate_presigned_url

        generate_presigned_url("k")
        call_kwargs = mock_client.generate_presigned_url.call_args
        assert call_kwargs[1]["ExpiresIn"] == 3600


# ---------------------------------------------------------------------------
# generate_presigned_upload_url
# ---------------------------------------------------------------------------


class TestGeneratePresignedUploadUrl:
    @patch("app.services.s3_storage._get_s3_config")
    @patch("app.services.s3_storage._get_s3_client")
    def test_returns_upload_url(self, mock_client_fn, mock_cfg):
        mock_cfg.return_value = _base_config()
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://upload-url"
        mock_client_fn.return_value = mock_client

        from app.services.s3_storage import generate_presigned_upload_url

        url = generate_presigned_upload_url("uploads/new.iso", expires=1800)
        assert url == "https://upload-url"
        mock_client.generate_presigned_url.assert_called_once_with(
            "put_object",
            Params={"Bucket": "test-bucket", "Key": "uploads/new.iso"},
            ExpiresIn=1800,
        )


# ---------------------------------------------------------------------------
# file_exists
# ---------------------------------------------------------------------------


class TestFileExists:
    @patch("app.services.s3_storage._bucket")
    @patch("app.services.s3_storage._get_s3_client")
    def test_returns_true_when_object_exists(self, mock_client_fn, mock_bucket):
        mock_client = MagicMock()
        mock_client.head_object.return_value = {"ContentLength": 100}
        mock_client_fn.return_value = mock_client
        mock_bucket.return_value = "test-bucket"

        from app.services.s3_storage import file_exists

        assert file_exists("library/exists.qcow2") is True
        mock_client.head_object.assert_called_once_with(
            Bucket="test-bucket", Key="library/exists.qcow2"
        )

    @patch("app.services.s3_storage._bucket")
    @patch("app.services.s3_storage._get_s3_client")
    def test_returns_false_on_client_error(self, mock_client_fn, mock_bucket):
        mock_client = MagicMock()
        # Set up the ClientError exception class on the mock client
        mock_client.exceptions.ClientError = ClientError
        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        mock_client_fn.return_value = mock_client
        mock_bucket.return_value = "test-bucket"

        from app.services.s3_storage import file_exists

        assert file_exists("library/missing.qcow2") is False


# ---------------------------------------------------------------------------
# _get_readonly_s3_config
# ---------------------------------------------------------------------------


class TestGetReadonlyS3Config:
    @patch("app.core.database.SessionLocal")
    def test_returns_config_when_provider_found(self, mock_sl):
        mock_provider = MagicMock()
        mock_provider.id = "prov-ro-1"
        mock_provider.default_region = "us-west-2"
        mock_provider.get_credentials.return_value = {
            "region": "us-west-2",
            "access_key_id": "AKIA_RO",
            "secret_access_key": "sec_ro",
            "bucket": "gold-images",
            "endpoint_url": "http://s4:9000",
        }
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_provider
        )
        mock_sl.return_value = mock_session

        from app.services.s3_storage import _get_readonly_s3_config

        cfg = _get_readonly_s3_config()
        assert cfg is not None
        assert cfg["provider_id"] == "prov-ro-1"
        assert cfg["access_key_id"] == "AKIA_RO"
        assert cfg["bucket"] == "gold-images"
        mock_session.close.assert_called()

    @patch("app.core.database.SessionLocal")
    def test_returns_none_when_no_provider(self, mock_sl):
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None
        mock_sl.return_value = mock_session

        from app.services.s3_storage import _get_readonly_s3_config

        assert _get_readonly_s3_config() is None
        mock_session.close.assert_called()

    @patch("app.core.database.SessionLocal")
    def test_returns_none_on_exception(self, mock_sl):
        mock_sl.side_effect = Exception("db down")

        from app.services.s3_storage import _get_readonly_s3_config

        assert _get_readonly_s3_config() is None


# ---------------------------------------------------------------------------
# _get_readonly_s3_client
# ---------------------------------------------------------------------------


class TestGetReadonlyS3Client:
    @patch("app.services.s3_storage.boto3")
    @patch("app.services.s3_storage._get_readonly_s3_config")
    def test_returns_client_when_config_found(self, mock_cfg, mock_boto):
        mock_cfg.return_value = {
            "region": "eu-central-1",
            "access_key_id": "AKIA_RO2",
            "secret_access_key": "sec_ro2",
            "bucket": "gold",
            "endpoint_url": "http://s4:9000",
        }
        mock_boto.client.return_value = MagicMock()

        from app.services.s3_storage import _get_readonly_s3_client

        client = _get_readonly_s3_client()
        assert client is not None
        mock_boto.client.assert_called_once_with(
            "s3",
            region_name="eu-central-1",
            aws_access_key_id="AKIA_RO2",
            aws_secret_access_key="sec_ro2",
            endpoint_url="http://s4:9000",
        )

    @patch("app.services.s3_storage._get_readonly_s3_config")
    def test_returns_none_when_no_config(self, mock_cfg):
        mock_cfg.return_value = None

        from app.services.s3_storage import _get_readonly_s3_client

        assert _get_readonly_s3_client() is None

    @patch("app.services.s3_storage.boto3")
    @patch("app.services.s3_storage._get_readonly_s3_config")
    def test_without_credentials(self, mock_cfg, mock_boto):
        """When access_key_id and endpoint_url are empty, they are not passed."""
        mock_cfg.return_value = {
            "region": "us-east-1",
            "access_key_id": "",
            "secret_access_key": "",
            "bucket": "gold",
            "endpoint_url": "",
        }
        mock_boto.client.return_value = MagicMock()

        from app.services.s3_storage import _get_readonly_s3_client

        _get_readonly_s3_client()
        call_kwargs = mock_boto.client.call_args[1]
        assert "aws_access_key_id" not in call_kwargs
        assert "aws_secret_access_key" not in call_kwargs
        assert "endpoint_url" not in call_kwargs


# ---------------------------------------------------------------------------
# generate_presigned_url_for_config
# ---------------------------------------------------------------------------


class TestGeneratePresignedUrlForConfig:
    @patch("app.services.s3_storage.boto3")
    def test_generates_url_with_custom_config(self, mock_boto):
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://custom-signed-url"
        mock_boto.client.return_value = mock_client

        from app.services.s3_storage import generate_presigned_url_for_config

        cfg = _base_config(bucket="custom-bucket", endpoint_url="http://s4:9000")
        url = generate_presigned_url_for_config(cfg, "library/item.qcow2", expires=900)

        assert url == "https://custom-signed-url"
        mock_boto.client.assert_called_once_with(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test-access-key-id",  # pragma: allowlist secret
            aws_secret_access_key="test-secret-key",  # pragma: allowlist secret
            endpoint_url="http://s4:9000",
        )
        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "custom-bucket", "Key": "library/item.qcow2"},
            ExpiresIn=900,
        )

    @patch("app.services.s3_storage.boto3")
    def test_without_credentials_or_endpoint(self, mock_boto):
        """When config has empty creds/endpoint, they are omitted from boto3."""
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://url"
        mock_boto.client.return_value = mock_client

        from app.services.s3_storage import generate_presigned_url_for_config

        cfg = _base_config(access_key_id="", secret_access_key="", endpoint_url="")
        generate_presigned_url_for_config(cfg, "key")

        call_kwargs = mock_boto.client.call_args[1]
        assert "aws_access_key_id" not in call_kwargs
        assert "aws_secret_access_key" not in call_kwargs
        assert "endpoint_url" not in call_kwargs

    @patch("app.services.s3_storage.boto3")
    def test_default_expiry(self, mock_boto):
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "https://url"
        mock_boto.client.return_value = mock_client

        from app.services.s3_storage import generate_presigned_url_for_config

        generate_presigned_url_for_config(_base_config(), "k")
        call_args = mock_client.generate_presigned_url.call_args
        assert call_args[1]["ExpiresIn"] == 3600
