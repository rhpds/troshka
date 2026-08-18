"""Tests for ocpvirt package-repo config resolution."""

from unittest.mock import MagicMock, patch

from app.core.ocpvirt_pkg_repo import resolve_pkg_repo


def test_resolve_pkg_repo_prefers_provider_creds():
    pkg = MagicMock(url="https://config/repo", username="cfg", password="cfg-pass")
    ocp = MagicMock(pkg_repo=pkg)
    cfg = MagicMock(ocpvirt=ocp)
    with patch("app.core.config.config", cfg):
        url, user, pw = resolve_pkg_repo(
            {
                "pkg_repo_url": "https://provider/repo",
                "pkg_repo_username": "prov",
                "pkg_repo_password": "prov-pass",
            }
        )
    assert url == "https://provider/repo"
    assert user == "prov"
    assert pw == "prov-pass"


def test_resolve_pkg_repo_falls_back_to_config():
    pkg = MagicMock(
        url="https://repo-troshka-images.apps.ocpv-infra01.dal12.infra.demo.redhat.com/rhel-10.2",
        username="troshka-repo",
        password="secret",
    )
    ocp = MagicMock(pkg_repo=pkg)
    cfg = MagicMock(ocpvirt=ocp)
    with patch("app.core.config.config", cfg):
        url, user, pw = resolve_pkg_repo({})
    assert "ocpv-infra01" in url
    assert user == "troshka-repo"
    assert pw == "secret"
