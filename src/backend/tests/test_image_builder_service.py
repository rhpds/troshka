import json
import time
from unittest.mock import MagicMock, patch

import pytest


def _make_provider(ptype="gcp"):
    p = MagicMock()
    p.id = "prov-1234"
    p.type = ptype
    p.gcp_project_id = "troshka-rhdp"
    p.azure_subscription_id = "sub-1234"
    p.azure_resource_group = "troshka-rg"
    p.azure_location = "eastus"
    p.default_image = None
    if ptype == "gcp":
        p.get_credentials.return_value = {
            "service_account_json": {
                "client_email": "troshka@troshka-rhdp.iam.gserviceaccount.com"
            }
        }
    elif ptype == "azure":
        p.get_credentials.return_value = {
            "tenant_id": "tenant-1234",
            "subscription_id": "sub-1234",
        }
    return p


def _make_user():
    u = MagicMock()
    u.id = "user-1234"
    u.rh_offline_token = "encrypted_token"
    return u


class TestTokenExchange:
    @patch("app.services.image_builder_service._http")
    def test_get_access_token(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = json.dumps(
            {"access_token": "bearer_tok_123", "expires_in": 900}
        ).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _exchange_token

        token = _exchange_token("my_offline_token")
        assert token == "bearer_tok_123"

        call_args = mock_http.request.call_args
        assert call_args[0][0] == "POST"
        assert "sso.redhat.com" in call_args[0][1]

    @patch("app.services.image_builder_service._http")
    def test_get_access_token_failure(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 401
        mock_resp.data = b'{"error": "invalid_grant"}'
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import (
            ImageBuilderError,
            _exchange_token,
        )

        with pytest.raises(ImageBuilderError, match="token exchange failed"):
            _exchange_token("bad_token")


class TestComposeRequest:
    @patch("app.services.image_builder_service._http")
    def test_start_compose_gcp(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.data = json.dumps({"id": "compose-uuid-123"}).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _start_compose

        compose_id = _start_compose(
            access_token="tok",
            distribution="rhel-10",
            image_type="gcp",
            upload_options={
                "share_with_accounts": ["troshka@project.iam.gserviceaccount.com"]
            },
        )
        assert compose_id == "compose-uuid-123"

        call_args = mock_http.request.call_args
        body = json.loads(call_args[1]["body"])
        assert body["distribution"] == "rhel-10"
        assert body["image_requests"][0]["image_type"] == "gcp"
        assert "qemu-kvm" in body["customizations"]["packages"]

    @patch("app.services.image_builder_service._http")
    def test_start_compose_azure(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.data = json.dumps({"id": "compose-uuid-456"}).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _start_compose

        compose_id = _start_compose(
            access_token="tok",
            distribution="rhel-10",
            image_type="azure",
            upload_options={
                "tenant_id": "t-1",
                "subscription_id": "s-1",
                "resource_group": "rg",
                "image_name": "troshka-host-rhel10",
            },
        )
        assert compose_id == "compose-uuid-456"

        body = json.loads(mock_http.request.call_args[1]["body"])
        assert body["image_requests"][0]["image_type"] == "azure"


class TestComposePolling:
    @patch("app.services.image_builder_service._http")
    def test_poll_compose_success(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = json.dumps(
            {
                "image_status": {
                    "status": "success",
                    "upload_status": {
                        "type": "gcp",
                        "options": {
                            "image_name": "composer-api-abc123",
                            "project_id": "red-hat-image-builder",
                        },
                    },
                }
            }
        ).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _poll_compose

        status = _poll_compose("tok", "compose-1")
        assert status["image_status"]["status"] == "success"

    @patch("app.services.image_builder_service._http")
    def test_poll_compose_building(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = json.dumps({"image_status": {"status": "building"}}).encode()
        mock_http.request.return_value = mock_resp

        from app.services.image_builder_service import _poll_compose

        status = _poll_compose("tok", "compose-1")
        assert status["image_status"]["status"] == "building"


class TestImageReference:
    def test_extract_gcp_image_ref(self):
        from app.services.image_builder_service import _extract_image_reference

        status = {
            "image_status": {
                "upload_status": {
                    "type": "gcp",
                    "options": {
                        "image_name": "composer-api-abc123",
                        "project_id": "red-hat-image-builder",
                    },
                }
            }
        }
        ref = _extract_image_reference(status, "gcp")
        assert ref == "projects/red-hat-image-builder/global/images/composer-api-abc123"

    def test_extract_azure_image_ref(self):
        from app.services.image_builder_service import _extract_image_reference

        status = {
            "image_status": {
                "upload_status": {
                    "type": "azure",
                    "options": {"image_name": "troshka-host-rhel10"},
                }
            }
        }
        provider = _make_provider("azure")
        ref = _extract_image_reference(status, "azure", provider=provider)
        assert "/resourceGroups/troshka-rg/" in ref
        assert ref.endswith("/troshka-host-rhel10")


class TestBuildUploadOptions:
    def test_gcp_upload_options(self):
        from app.services.image_builder_service import _build_upload_options

        provider = _make_provider("gcp")
        opts = _build_upload_options(provider)
        assert opts["share_with_accounts"] == [
            "troshka@troshka-rhdp.iam.gserviceaccount.com"
        ]

    def test_azure_upload_options(self):
        from app.services.image_builder_service import _build_upload_options

        provider = _make_provider("azure")
        opts = _build_upload_options(provider)
        assert opts["tenant_id"] == "tenant-1234"
        assert opts["subscription_id"] == "sub-1234"
        assert opts["resource_group"] == "troshka-rg"
        assert "troshka-host-rhel" in opts["image_name"]
