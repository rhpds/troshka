"""OCP pull-secret validate endpoint and save-time probe."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


def test_validate_user_pull_credentials_missing():
    from app.services.ocp.pull_through_registry import (
        OCP_PULL_SECRET_DOWNLOAD_URL,
        validate_user_pull_credentials,
    )

    err = validate_user_pull_credentials(SimpleNamespace(ocp_pull_secret=None))
    assert err is not None
    assert OCP_PULL_SECRET_DOWNLOAD_URL in err


def test_get_ocp_pull_secret_validate_reports_invalid(monkeypatch):
    from app.api import auth as api

    user = SimpleNamespace(
        ocp_pull_secret="enc",
        pull_through_registry=False,
        pull_through_registry_url=None,
    )
    monkeypatch.setattr(
        "app.core.encryption.decrypt",
        lambda *_: '{"auths":{"quay.io":{"auth":"dXNlcjpwYXNz"}}}',
    )
    with patch(
        "app.services.ocp.pull_through_registry.validate_user_pull_credentials",
        return_value="OCP pull secret is invalid or expired.",
    ):
        result = api.get_ocp_pull_secret(user, validate=True)
    assert result["has_secret"] is True
    assert result["valid"] is False
    assert "invalid or expired" in result["error"]


def test_set_ocp_pull_secret_rejects_invalid_quay_secret(monkeypatch):
    from app.api import auth as api
    from app.services.ocp.pull_through_registry import OcpPullSecretError

    user = SimpleNamespace(
        ocp_pull_secret=None,
        pull_through_registry=False,
        pull_through_registry_url=None,
        pull_through_registry_user=None,
        pull_through_registry_password=None,
    )
    db = MagicMock()
    with patch(
        "app.services.ocp.pull_through_registry.probe_ocp_pull_secret",
        side_effect=OcpPullSecretError("bad secret"),
    ):
        with pytest.raises(HTTPException) as exc:
            api.set_ocp_pull_secret(
                {"pull_secret": '{"auths":{"quay.io":{"auth":"dXNlcjpwYXNz"}}}'},
                user,
                db,
            )
    assert exc.value.status_code == 400
    assert "bad secret" in exc.value.detail
    db.commit.assert_not_called()
