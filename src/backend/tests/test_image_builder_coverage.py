"""Tests for uncovered functions in image_builder_service.

Covers: get_build_status, clear_build_status, _set_build_error,
_resolve_build_inputs, _poll_with_token_refresh, _handle_compose_result,
_poll_compose_loop, build_host_image, _api_request, _extract_image_reference
(unknown provider), _build_upload_options (unsupported provider).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import app.services.image_builder_service as ibs
from app.services.image_builder_service import (
    ImageBuilderError,
    _api_request,
    _build_upload_options,
    _extract_image_reference,
    _handle_compose_result,
    _poll_compose_loop,
    _poll_with_token_refresh,
    _resolve_build_inputs,
    _set_build_error,
    build_host_image,
    clear_build_status,
    get_build_status,
)


def _make_provider(ptype="gcp"):
    p = MagicMock()
    p.id = "prov-1234"
    p.type = ptype
    p.name = "test-provider"
    p.gcp_project_id = "troshka-rhdp"
    p.azure_subscription_id = "sub-1234"
    p.azure_resource_group = "troshka-rg"
    p.default_image = None
    if ptype == "gcp":
        p.get_credentials.return_value = {
            "service_account_json": {"client_email": "sa@proj.iam.gserviceaccount.com"}
        }
    elif ptype == "azure":
        p.get_credentials.return_value = {
            "tenant_id": "tenant-1234",
            "subscription_id": "sub-1234",
        }
    return p


@pytest.fixture(autouse=True)
def _clean_build_progress():
    """Save and restore _build_progress around each test."""
    saved = dict(ibs._build_progress)
    ibs._build_progress.clear()
    yield
    ibs._build_progress.clear()
    ibs._build_progress.update(saved)


# ── get_build_status ──


class TestGetBuildStatus:
    def test_no_entry_returns_idle(self):
        result = get_build_status("nonexistent-provider")
        assert result == {"status": "idle", "message": "", "image": None}

    def test_existing_entry_returned(self):
        ibs._build_progress["prov-1"] = {
            "status": "building",
            "message": "Step 1/3",
        }
        result = get_build_status("prov-1")
        assert result["status"] == "building"
        assert result["message"] == "Step 1/3"


# ── clear_build_status ──


class TestClearBuildStatus:
    def test_clear_existing_entry(self):
        ibs._build_progress["prov-1"] = {"status": "error", "message": "fail"}
        clear_build_status("prov-1")
        assert "prov-1" not in ibs._build_progress

    def test_clear_nonexistent_no_error(self):
        clear_build_status("does-not-exist")  # should not raise


# ── _set_build_error ──


class TestSetBuildError:
    def test_basic_error(self):
        _set_build_error("prov-1", "Something went wrong")
        assert ibs._build_progress["prov-1"]["status"] == "error"
        assert ibs._build_progress["prov-1"]["message"] == "Something went wrong"

    def test_with_extra_kwargs(self):
        _set_build_error("prov-2", "Timed out", compose_id="c-123", elapsed_seconds=999)
        entry = ibs._build_progress["prov-2"]
        assert entry["status"] == "error"
        assert entry["compose_id"] == "c-123"
        assert entry["elapsed_seconds"] == 999


# ── _resolve_build_inputs ──


class TestResolveBuildInputs:
    @patch("app.core.encryption.decrypt", return_value="decrypted_tok")
    def test_success(self, _mock_decrypt):
        db = MagicMock()
        provider = _make_provider()
        user = MagicMock()
        user.rh_offline_token = "encrypted"
        db.get.side_effect = lambda model, id_: (
            provider if model.__name__ == "Provider" else user
        )

        result = _resolve_build_inputs(db, "prov-1", "user-1")
        assert result is not None
        prov_out, tok_out = result
        assert prov_out is provider
        assert tok_out == "decrypted_tok"

    def test_provider_not_found(self):
        db = MagicMock()
        db.get.return_value = None

        result = _resolve_build_inputs(db, "prov-missing", "user-1")
        assert result is None
        assert ibs._build_progress["prov-missing"]["status"] == "error"
        assert "not found" in ibs._build_progress["prov-missing"]["message"]

    def test_user_not_found(self):
        db = MagicMock()
        provider = _make_provider()
        db.get.side_effect = lambda model, id_: (
            provider if model.__name__ == "Provider" else None
        )

        result = _resolve_build_inputs(db, "prov-1", "user-missing")
        assert result is None
        assert ibs._build_progress["prov-1"]["status"] == "error"

    @patch("app.core.encryption.decrypt", return_value="decrypted_tok")
    def test_no_offline_token(self, _mock_decrypt):
        db = MagicMock()
        provider = _make_provider()
        user = MagicMock()
        user.rh_offline_token = None
        db.get.side_effect = lambda model, id_: (
            provider if model.__name__ == "Provider" else user
        )

        result = _resolve_build_inputs(db, "prov-1", "user-1")
        assert result is None
        assert "offline token" in ibs._build_progress["prov-1"]["message"]

    @patch("app.core.encryption.decrypt", return_value="")
    def test_decrypt_returns_empty(self, _mock_decrypt):
        db = MagicMock()
        provider = _make_provider()
        user = MagicMock()
        user.rh_offline_token = "encrypted"
        db.get.side_effect = lambda model, id_: (
            provider if model.__name__ == "Provider" else user
        )

        result = _resolve_build_inputs(db, "prov-1", "user-1")
        assert result is None
        assert "decrypt" in ibs._build_progress["prov-1"]["message"].lower()

    @patch("app.core.encryption.decrypt", return_value=None)
    def test_decrypt_returns_none(self, _mock_decrypt):
        db = MagicMock()
        provider = _make_provider()
        user = MagicMock()
        user.rh_offline_token = "encrypted"
        db.get.side_effect = lambda model, id_: (
            provider if model.__name__ == "Provider" else user
        )

        result = _resolve_build_inputs(db, "prov-1", "user-1")
        assert result is None


# ── _poll_with_token_refresh ──


class TestPollWithTokenRefresh:
    @patch("app.services.image_builder_service._poll_compose")
    def test_success_first_try(self, mock_poll):
        mock_poll.return_value = {"image_status": {"status": "building"}}
        status, token = _poll_with_token_refresh("tok", "offline", "c-1")
        assert status["image_status"]["status"] == "building"
        assert token == "tok"
        assert mock_poll.call_count == 1

    @patch("app.services.image_builder_service._exchange_token", return_value="new_tok")
    @patch("app.services.image_builder_service._poll_compose")
    def test_401_triggers_refresh(self, mock_poll, mock_exchange):
        mock_poll.side_effect = [
            ImageBuilderError("API error (HTTP 401): unauthorized"),
            {"image_status": {"status": "building"}},
        ]
        status, token = _poll_with_token_refresh("old_tok", "offline", "c-1")
        assert token == "new_tok"
        assert mock_poll.call_count == 2
        mock_exchange.assert_called_once_with("offline")

    @patch("app.services.image_builder_service._poll_compose")
    def test_non_auth_error_raised(self, mock_poll):
        mock_poll.side_effect = ImageBuilderError("API error (HTTP 500): server error")
        with pytest.raises(ImageBuilderError, match="500"):
            _poll_with_token_refresh("tok", "offline", "c-1")

    @patch("app.services.image_builder_service._exchange_token", return_value="new_tok")
    @patch("app.services.image_builder_service._poll_compose")
    def test_403_triggers_refresh(self, mock_poll, mock_exchange):
        mock_poll.side_effect = [
            ImageBuilderError("API error (HTTP 403): forbidden"),
            {"image_status": {"status": "success"}},
        ]
        status, token = _poll_with_token_refresh("old_tok", "offline", "c-1")
        assert token == "new_tok"


# ── _handle_compose_result ──


class TestHandleComposeResult:
    def _success_status(self):
        return {
            "image_status": {
                "status": "success",
                "upload_status": {
                    "type": "gcp",
                    "options": {
                        "image_name": "built-image",
                        "project_id": "red-hat-image-builder",
                    },
                },
            }
        }

    def test_success_sets_default_image(self):
        provider = _make_provider("gcp")
        db = MagicMock()
        done = _handle_compose_result(
            self._success_status(), provider, "prov-1", "c-1", 300, db
        )
        assert done is True
        assert provider.default_image is not None
        db.commit.assert_called_once()
        assert ibs._build_progress["prov-1"]["status"] == "success"
        assert ibs._build_progress["prov-1"]["compose_id"] == "c-1"

    def test_failure_with_error_details(self):
        status = {
            "image_status": {
                "status": "failure",
                "error": {
                    "reason": "Quota exceeded",
                    "details": "Not enough disk in us-east1",
                },
            }
        }
        provider = _make_provider("gcp")
        db = MagicMock()
        done = _handle_compose_result(status, provider, "prov-1", "c-1", 600, db)
        assert done is True
        assert ibs._build_progress["prov-1"]["status"] == "error"
        assert "Quota exceeded" in ibs._build_progress["prov-1"]["message"]

    def test_failure_without_details(self):
        status = {
            "image_status": {
                "status": "failure",
                "error": {"reason": "Internal error"},
            }
        }
        provider = _make_provider("gcp")
        db = MagicMock()
        done = _handle_compose_result(status, provider, "prov-1", "c-1", 100, db)
        assert done is True
        assert "Internal error" in ibs._build_progress["prov-1"]["message"]

    def test_still_building_returns_false(self):
        status = {"image_status": {"status": "building"}}
        provider = _make_provider("gcp")
        db = MagicMock()
        done = _handle_compose_result(status, provider, "prov-1", "c-1", 200, db)
        assert done is False

    def test_timeout_after_3600s(self):
        status = {"image_status": {"status": "building"}}
        provider = _make_provider("gcp")
        db = MagicMock()
        done = _handle_compose_result(status, provider, "prov-1", "c-1", 3601, db)
        assert done is True
        assert ibs._build_progress["prov-1"]["status"] == "error"
        assert "timed out" in ibs._build_progress["prov-1"]["message"].lower()


# ── _poll_compose_loop ──


class TestPollComposeLoop:
    @patch(
        "app.services.image_builder_service._handle_compose_result", return_value=True
    )
    @patch(
        "app.services.image_builder_service._poll_with_token_refresh",
        return_value=(
            {
                "image_status": {
                    "status": "success",
                    "progress": {"done": 3, "total": 3},
                }
            },
            "tok",
        ),
    )
    @patch("app.services.image_builder_service.time")
    def test_loop_exits_on_first_poll(self, mock_time, mock_poll, mock_handle):
        mock_time.sleep = MagicMock()
        mock_time.time.return_value = 100.0

        ibs._build_progress["prov-1"] = {"status": "building", "message": ""}

        _poll_compose_loop(
            "tok", "offline", "c-1", _make_provider(), "prov-1", 70.0, MagicMock()
        )

        mock_time.sleep.assert_called_once_with(30)
        mock_handle.assert_called_once()

    @patch("app.services.image_builder_service._handle_compose_result")
    @patch("app.services.image_builder_service._poll_with_token_refresh")
    @patch("app.services.image_builder_service.time")
    def test_loop_runs_multiple_iterations(self, mock_time, mock_poll, mock_handle):
        mock_time.sleep = MagicMock()
        mock_time.time.return_value = 100.0

        building_status = {
            "image_status": {
                "status": "building",
                "progress": {"done": 1, "total": 3},
            }
        }
        success_status = {
            "image_status": {
                "status": "success",
                "progress": {"done": 3, "total": 3},
            }
        }
        mock_poll.side_effect = [
            (building_status, "tok"),
            (success_status, "tok"),
        ]
        mock_handle.side_effect = [False, True]

        ibs._build_progress["prov-1"] = {"status": "building", "message": ""}

        _poll_compose_loop(
            "tok", "offline", "c-1", _make_provider(), "prov-1", 70.0, MagicMock()
        )

        assert mock_handle.call_count == 2

    @patch(
        "app.services.image_builder_service._handle_compose_result", return_value=True
    )
    @patch("app.services.image_builder_service._poll_with_token_refresh")
    @patch("app.services.image_builder_service.time")
    def test_loop_no_progress_total(self, mock_time, mock_poll, mock_handle):
        """When progress has no total field, message uses simple format."""
        mock_time.sleep = MagicMock()
        mock_time.time.return_value = 130.0

        status_no_total = {"image_status": {"status": "building", "progress": {}}}
        mock_poll.return_value = (status_no_total, "tok")

        ibs._build_progress["prov-1"] = {"status": "building", "message": ""}

        _poll_compose_loop(
            "tok", "offline", "c-1", _make_provider(), "prov-1", 70.0, MagicMock()
        )

        msg = ibs._build_progress["prov-1"]["message"]
        assert "elapsed" in msg


# ── build_host_image ──


class TestBuildHostImage:
    @patch("app.services.image_builder_service._poll_compose_loop")
    @patch(
        "app.services.image_builder_service._start_compose", return_value="compose-999"
    )
    @patch(
        "app.services.image_builder_service._build_upload_options",
        return_value={"share_with_accounts": ["sa@proj.iam"]},
    )
    @patch(
        "app.services.image_builder_service._exchange_token", return_value="access_tok"
    )
    @patch("app.services.image_builder_service._resolve_build_inputs")
    @patch("app.core.database.SessionLocal")
    def test_success_path(
        self,
        mock_session_cls,
        mock_resolve,
        mock_exchange,
        mock_upload,
        mock_start,
        mock_poll_loop,
    ):
        provider = _make_provider("gcp")
        mock_resolve.return_value = (provider, "offline_tok")
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        build_host_image("prov-1", "user-1", "rhel-10")

        mock_resolve.assert_called_once()
        mock_exchange.assert_called_once_with("offline_tok")
        mock_start.assert_called_once()
        mock_poll_loop.assert_called_once()
        mock_db.close.assert_called_once()
        assert ibs._build_progress["prov-1"]["compose_id"] == "compose-999"

    @patch(
        "app.services.image_builder_service._resolve_build_inputs", return_value=None
    )
    @patch("app.core.database.SessionLocal")
    def test_resolve_returns_none(self, mock_session_cls, mock_resolve):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        build_host_image("prov-1", "user-1")

        mock_db.close.assert_called_once()
        # Should have set initial progress, then returned early
        assert ibs._build_progress["prov-1"]["status"] == "authenticating"

    @patch(
        "app.services.image_builder_service._resolve_build_inputs",
        side_effect=RuntimeError("DB exploded"),
    )
    @patch("app.core.database.SessionLocal")
    def test_exception_path(self, mock_session_cls, mock_resolve):
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        build_host_image("prov-1", "user-1")

        assert ibs._build_progress["prov-1"]["status"] == "error"
        assert "DB exploded" in ibs._build_progress["prov-1"]["message"]
        mock_db.close.assert_called_once()


# ── _api_request ──


class TestApiRequest:
    @patch("app.services.image_builder_service._http")
    def test_success_with_body(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = json.dumps({"result": "ok"}).encode()
        mock_http.request.return_value = mock_resp

        result = _api_request("POST", "/compose", "bearer_tok", {"key": "val"})
        assert result == {"result": "ok"}

        call_args = mock_http.request.call_args
        assert call_args[0][0] == "POST"
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer bearer_tok"
        assert headers["Content-Type"] == "application/json"
        assert call_args[1]["body"] is not None

    @patch("app.services.image_builder_service._http")
    def test_success_without_body(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = json.dumps({"id": "c-1"}).encode()
        mock_http.request.return_value = mock_resp

        result = _api_request("GET", "/composes/c-1", "bearer_tok")
        assert result["id"] == "c-1"

        call_args = mock_http.request.call_args
        headers = call_args[1]["headers"]
        assert "Content-Type" not in headers
        assert call_args[1]["body"] is None

    @patch("app.services.image_builder_service._http")
    def test_error_response(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.data = b"Internal Server Error"
        mock_http.request.return_value = mock_resp

        with pytest.raises(ImageBuilderError, match="HTTP 500"):
            _api_request("GET", "/composes/c-1", "tok")


# ── _extract_image_reference edge cases ──


class TestExtractImageReferenceEdge:
    def test_unknown_provider_type_raises(self):
        status = {
            "image_status": {
                "upload_status": {
                    "type": "aws",
                    "options": {"ami_id": "ami-123"},
                }
            }
        }
        with pytest.raises(ImageBuilderError, match="Unknown provider type"):
            _extract_image_reference(status, "aws")


# ── _build_upload_options edge cases ──


class TestBuildUploadOptionsEdge:
    def test_unsupported_provider_type_raises(self):
        provider = MagicMock()
        provider.type = "ec2"
        with pytest.raises(ImageBuilderError, match="Unsupported provider type"):
            _build_upload_options(provider)
