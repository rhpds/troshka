"""Tests for OCP client mirror URL routing."""

import types

from app.api import ocp_versions as ocp_versions_api
from app.services.ocp import client_mirror as cm
from app.services.ocp.ops_pod_install import _ensure_installers_cmd


def test_installer_tarball_url_4x_stable():
    url = cm.installer_tarball_url("4.22", "openshift-install-linux.tar.gz")
    assert url == (
        "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/"
        "stable-4.22/openshift-install-linux.tar.gz"
    )


def test_installer_tarball_url_5x_dev_preview():
    url = cm.installer_tarball_url("5.0", "openshift-client-linux.tar.gz")
    assert url == (
        "https://mirror.openshift.com/pub/openshift-v4/clients/ocp-dev-preview/"
        "latest/openshift-client-linux.tar.gz"
    )


def test_installer_tarball_url_okd_scos(monkeypatch):
    monkeypatch.setattr(cm, "resolve_okd_scos_tag", lambda v: "4.22.0-okd-scos.9")
    url = cm.installer_tarball_url(
        "4.22", "openshift-install-linux.tar.gz", distribution="okd-scos"
    )
    assert url == (
        "https://github.com/okd-project/okd/releases/download/"
        "4.22.0-okd-scos.9/openshift-install-linux-4.22.0-okd-scos.9.tar.gz"
    )


def test_okd_scos_tag_pick_prefers_stable():
    tags = [
        "4.22.0-okd-scos.ec.3",
        "4.22.0-okd-scos.8",
        "4.22.0-okd-scos.9",
        "5.0.0-okd-scos.0",
    ]
    assert cm._pick_okd_scos_tag(tags, 4, 22) == "4.22.0-okd-scos.9"


def test_resolve_okd_scos_tag_uses_fallback(monkeypatch):
    cm.resolve_okd_scos_tag.cache_clear()
    monkeypatch.setattr(
        cm,
        "_fetch_okd_release_tags",
        lambda pages=3: (_ for _ in ()).throw(OSError("offline")),
    )
    assert cm.resolve_okd_scos_tag("4.22") == "4.22.0-okd-scos.9"
    cm.resolve_okd_scos_tag.cache_clear()


def test_normalize_distribution_aliases():
    assert cm.normalize_distribution("okd") == "okd-scos"
    assert cm.normalize_distribution("OKD_SCOS") == "okd-scos"
    assert cm.normalize_distribution(None) == "ocp"
    assert cm.is_okd_scos("scos") is True
    assert cm.default_pull_secret_for_distribution("okd-scos") == "{}"
    assert cm.default_pull_secret_for_distribution("ocp") == ""


def test_parse_ocp_version_defaults_on_garbage():
    assert cm.parse_ocp_version("") == (4, 22)
    assert cm.parse_ocp_version("not-a-version") == (4, 22)


def test_preview_version_entries_respects_flag(monkeypatch):
    monkeypatch.setattr(cm, "preview_versions_enabled", lambda: False)
    assert cm.preview_version_entries() == []

    monkeypatch.setattr(cm, "preview_versions_enabled", lambda: True)
    assert cm.preview_version_entries() == [{"name": "5.0", "support": "Dev Preview"}]


def test_preview_versions_enabled_from_config(monkeypatch):
    fake = types.SimpleNamespace(
        ocp=types.SimpleNamespace(preview_versions_enabled=True),
        auth=types.SimpleNamespace(oauth_enabled=True),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.core.config",
        types.SimpleNamespace(config=fake),
    )
    assert cm.preview_versions_enabled() is True


def test_preview_versions_enabled_dev_mode(monkeypatch):
    fake = types.SimpleNamespace(
        ocp=types.SimpleNamespace(),
        auth=types.SimpleNamespace(oauth_enabled=False),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.core.config",
        types.SimpleNamespace(config=fake),
    )
    assert cm.preview_versions_enabled() is True


def test_merge_preview_in_ocp_versions_api(monkeypatch):
    monkeypatch.setattr(
        ocp_versions_api,
        "preview_version_entries",
        lambda: [{"name": "5.0", "support": "Dev Preview"}],
    )
    merged = ocp_versions_api._merge_preview_versions(
        [{"name": "4.22", "support": "Full Support"}]
    )
    assert merged[0]["name"] == "5.0"
    assert merged[1]["name"] == "4.22"


def test_ensure_installers_cmd_5_0():
    script = _ensure_installers_cmd("5.0")
    assert "ocp-dev-preview/latest/openshift-install-linux.tar.gz" in script
    assert "Downloading openshift-install 5.0" in script
    assert "Dev-preview: EE image ships GA 4.x clients" in script
    assert "if true; then" in script
    assert "if ! command -v openshift-install" not in script


def test_ensure_installers_cmd_4x_skips_when_present():
    script = _ensure_installers_cmd("4.22")
    assert "if ! command -v openshift-install >/dev/null 2>&1; then" in script
    assert "dev-preview" not in script


def test_ensure_installers_cmd_okd_scos(monkeypatch):
    monkeypatch.setattr(cm, "resolve_okd_scos_tag", lambda v: "4.22.0-okd-scos.9")
    script = _ensure_installers_cmd("4.22", distribution="okd-scos")
    assert "github.com/okd-project/okd/releases/download/4.22.0-okd-scos.9/" in script
    assert "OKD/SCOS: EE image ships OCP clients" in script
    assert "if true; then" in script
