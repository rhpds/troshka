"""Tests for OBC helper — ensure_obc and get_obc_s3_config."""

import base64
from unittest.mock import MagicMock, patch

import pytest

from helpers.obc import OBC_NAME, RGW_ENDPOINT, ensure_obc, get_obc_s3_config


class _FakeApiException(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"({status})")


@pytest.fixture(autouse=True)
def _patch_api_exception():
    with patch("helpers.obc.ApiException", _FakeApiException):
        yield


def _mock_secret(access_key="AKTEST", secret_key="SKTEST"):  # pragma: allowlist secret
    s = MagicMock()
    s.data = {
        "AWS_ACCESS_KEY_ID": base64.b64encode(access_key.encode()).decode(),
        "AWS_SECRET_ACCESS_KEY": base64.b64encode(secret_key.encode()).decode(),
    }
    return s


def _mock_configmap(bucket="troshka-patterns-abc123", region=""):
    cm = MagicMock()
    cm.data = {"BUCKET_NAME": bucket, "BUCKET_REGION": region}
    return cm


class TestGetObcS3Config:
    def test_returns_config_when_secret_and_cm_exist(self):
        core_api = MagicMock()
        core_api.read_namespaced_secret.return_value = _mock_secret()
        core_api.read_namespaced_config_map.return_value = _mock_configmap()

        result = get_obc_s3_config(core_api)
        assert result is not None
        assert result["bucket"] == "troshka-patterns-abc123"
        assert result["endpoint"] == RGW_ENDPOINT
        assert result["access_key_id"] == "AKTEST"
        assert result["secret_access_key"] == "SKTEST"  # pragma: allowlist secret
        assert result["region"] == "us-east-1"
        assert result["credentials_secret"] == OBC_NAME

    def test_returns_none_when_secret_missing(self):
        core_api = MagicMock()
        core_api.read_namespaced_secret.side_effect = _FakeApiException(404)

        result = get_obc_s3_config(core_api)
        assert result is None

    def test_region_defaults_to_us_east_1(self):
        core_api = MagicMock()
        core_api.read_namespaced_secret.return_value = _mock_secret()
        core_api.read_namespaced_config_map.return_value = _mock_configmap(region="")

        result = get_obc_s3_config(core_api)
        assert result["region"] == "us-east-1"


class TestEnsureObc:
    def test_creates_obc_when_not_found(self):
        custom_api = MagicMock()
        core_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = _FakeApiException(404)
        core_api.read_namespaced_secret.return_value = _mock_secret()
        core_api.read_namespaced_config_map.return_value = _mock_configmap()

        result = ensure_obc(custom_api, core_api)
        custom_api.create_namespaced_custom_object.assert_called_once()
        assert result is not None
        assert result["bucket"] == "troshka-patterns-abc123"

    def test_skips_creation_when_already_exists(self):
        custom_api = MagicMock()
        core_api = MagicMock()
        custom_api.get_namespaced_custom_object.return_value = {"metadata": {}}
        core_api.read_namespaced_secret.return_value = _mock_secret()
        core_api.read_namespaced_config_map.return_value = _mock_configmap()

        result = ensure_obc(custom_api, core_api)
        custom_api.create_namespaced_custom_object.assert_not_called()
        assert result is not None

    def test_raises_on_non_404_error(self):
        custom_api = MagicMock()
        core_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = _FakeApiException(500)

        with pytest.raises(_FakeApiException):
            ensure_obc(custom_api, core_api)
