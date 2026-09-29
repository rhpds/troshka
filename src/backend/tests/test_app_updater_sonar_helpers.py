"""Unit tests for app_updater pure helpers (Sonar new-code coverage)."""

from unittest.mock import patch

from app.services import app_updater as au


def test_is_commit_sha_tag():
    assert au._is_commit_sha_tag("abcdef0123456789abcdef0123456789abcdef01")
    assert not au._is_commit_sha_tag("latest")
    assert not au._is_commit_sha_tag(None)


def test_comparison_tag_maps_sha_to_rolling():
    with patch.object(au, "_rolling_tag", return_value="main"):
        assert au._comparison_tag("abcdef0123456789abcdef0123456789abcdef01") == "main"
        assert au._comparison_tag("v1.2.3") == "v1.2.3"


def test_extract_tag_from_ref():
    assert au._extract_tag_from_ref("quay.io/org/img:stable") == "stable"
    assert au._extract_tag_from_ref("no-tag") is None
    assert au._extract_tag_from_ref("") is None


def test_is_argo_managed():
    assert au._is_argo_managed({"argocd.argoproj.io/instance": "x"})
    assert au._is_argo_managed({}, {"argocd.argoproj.io/tracking-id": "x"})
    assert not au._is_argo_managed({}, {})


def test_selector_from_match_labels():
    sel = au._selector_from_match_labels({"a": "1", "b": "2"})
    assert "a=1" in sel and "b=2" in sel
    assert au._selector_from_match_labels(None) == ""


def test_configured_mode_default():
    with patch.object(au, "_au", return_value="manual"):
        assert au._configured_mode() == "manual"
    with patch.object(au, "_au", return_value=None):
        assert au._configured_mode() == "auto"
