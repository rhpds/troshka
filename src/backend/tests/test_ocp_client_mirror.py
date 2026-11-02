"""Tests for OCP client mirror URL routing."""

import subprocess
import types
from io import BytesIO

import pytest

from app.api import ocp_versions as ocp_versions_api
from app.services.ocp import client_mirror as cm
from app.services.ocp.ops_pod_install import _ensure_installers_cmd


def test_installer_tarball_url_4x_stable():
    url = cm.installer_tarball_url("4.22", "openshift-install-linux.tar.gz")
    assert url == (
        "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/stable-4.22/openshift-install-linux.tar.gz"
    )


def test_installer_tarball_url_5x_dev_preview():
    url = cm.installer_tarball_url("5.0.0-ec.6", "openshift-client-linux.tar.gz")
    assert url == (
        "https://mirror.openshift.com/pub/openshift-v4/clients/ocp-dev-preview/5.0.0-ec.6/openshift-client-linux.tar.gz"
    )


def test_minor_preview_never_crosses_into_next_minor(monkeypatch):
    monkeypatch.setattr(
        cm,
        "available_mirror_versions",
        lambda preview: [
            "5.1.0-ec.1",
            "5.0.0-ec.10",
            "5.0.0-ec.6",
        ],
    )
    assert cm.installer_tarball_url("5.0", "openshift-install-linux.tar.gz").endswith(
        "/5.0.0-ec.10/openshift-install-linux.tar.gz"
    )
    with pytest.raises(ValueError, match="5.2"):
        cm.installer_tarball_url("5.2", "openshift-install-linux.tar.gz")


def test_exact_ga_release_does_not_fall_back_to_default_minor():
    assert cm.installer_tarball_url(
        "4.20.12", "openshift-install-linux.tar.gz"
    ).endswith("/ocp/4.20.12/openshift-install-linux.tar.gz")
    assert cm.installer_tarball_url("5.0.0", "openshift-install-linux.tar.gz").endswith(
        "/ocp/5.0.0/openshift-install-linux.tar.gz"
    )


def test_catalog_filters_aliases_and_sorts_release_numbers(monkeypatch):
    monkeypatch.setattr(cm, "_mirror_cache", {})
    body = b"""<a href="latest/">latest</a><a href="candidate-5.0/">alias</a>
    <a href="5.0.0-ec.9/">old</a><a href="5.0.0-ec.10/">new</a>
    <a href="5.1.0-ec.1/">next</a><a href="4.22.0-rc.1/">old major</a>
    <a href="https://evil.example/5.2.0/">external</a>
    <a href="5.0.0-ec.10/">duplicate</a>"""
    monkeypatch.setattr(cm.urllib.request, "urlopen", lambda *a, **k: BytesIO(body))
    assert cm.available_mirror_versions(True) == [
        "5.1.0-ec.1",
        "5.0.0-ec.10",
        "5.0.0-ec.9",
    ]


def test_catalog_outage_serves_known_releases_only(monkeypatch):
    monkeypatch.setattr(cm, "_mirror_cache", {})

    def offline(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(cm.urllib.request, "urlopen", offline)
    assert cm.available_mirror_versions(True) == []
    monkeypatch.setattr(cm, "_mirror_cache", {True: (0, ["5.0.0-ec.6"])})
    assert cm.available_mirror_versions(True) == ["5.0.0-ec.6"]


@pytest.mark.parametrize(
    ("selected", "installed", "success"),
    [
        ("5.0.0-ec.6", "5.0.0-ec.6", True),
        ("5.0.0-ec.6", "5.1.0-ec.1", False),
        ("5.0", "5.1.0-ec.1", False),
        ("5.0", "5.0.0-ec.6", True),
        ("4.20.12", "4.20.1", False),
    ],
)
def test_installer_version_guard_rejects_wrong_release(selected, installed, success):
    script = f"installer() {{ echo 'openshift-install {installed}'; }}\n"
    script += cm.installer_version_check_cmd(selected, "installer")
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert (result.returncode == 0) is success


def test_installer_tarball_url_okd_scos(monkeypatch):
    monkeypatch.setattr(cm, "resolve_okd_scos_tag", lambda v: "4.22.0-okd-scos.9")
    url = cm.installer_tarball_url(
        "4.22", "openshift-install-linux.tar.gz", distribution="okd-scos"
    )
    assert url == (
        "https://github.com/okd-project/okd/releases/download/"
        "4.22.0-okd-scos.9/openshift-install-linux-4.22.0-okd-scos.9.tar.gz"
    )


def test_legacy_minor_prefers_available_ga_over_preview(monkeypatch):
    monkeypatch.setattr(
        cm,
        "available_mirror_versions",
        lambda preview: ["5.1.0-ec.1", "5.0.0-ec.6"] if preview else ["5.0.0"],
    )
    assert cm.installer_tarball_url("5.0", "openshift-install-linux.tar.gz").endswith(
        "/ocp/5.0.0/openshift-install-linux.tar.gz"
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
    monkeypatch.setattr(
        cm, "available_mirror_versions", lambda preview: ["5.1.0-ec.1", "5.0.0-ec.6"]
    )
    monkeypatch.setattr(cm, "preview_versions_enabled", lambda: False)
    assert cm.preview_version_entries() == []

    monkeypatch.setattr(cm, "preview_versions_enabled", lambda: True)
    assert cm.preview_version_entries() == [
        {"name": "5.1.0-ec.1", "support": "Dev Preview"},
        {"name": "5.0.0-ec.6", "support": "Dev Preview"},
    ]


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
        lambda: [{"name": "5.0.0-ec.6", "support": "Dev Preview"}],
    )
    merged = ocp_versions_api._merge_preview_versions(
        [{"name": "4.22", "support": "Full Support"}]
    )
    assert merged[0]["name"] == "5.0.0-ec.6"
    assert merged[1]["name"] == "4.22"


def test_version_api_lists_available_patches_for_supported_minors(monkeypatch):
    monkeypatch.setattr(
        cm,
        "available_mirror_versions",
        lambda preview: [
            "4.20.10",
            "4.20.9",
            "4.19.7",
            "4.18.1",
        ],
    )
    monkeypatch.setattr(ocp_versions_api, "preview_version_entries", lambda: [])
    payload = {
        "data": [
            {
                "versions": [
                    {"name": "4.20", "type": "Full Support"},
                    {"name": "4.19", "type": "Maintenance Support"},
                    {"name": "4.18", "type": "End of Life"},
                ]
            }
        ]
    }
    assert ocp_versions_api._parse_versions(payload) == [
        {"name": "4.20.10", "support": "Full Support"},
        {"name": "4.20.9", "support": "Full Support"},
        {"name": "4.19.7", "support": "Maintenance Support"},
    ]


def test_ensure_installers_cmd_5_0():
    script = _ensure_installers_cmd("5.0.0-ec.6")
    assert "ocp-dev-preview/5.0.0-ec.6/openshift-install-linux.tar.gz" in script
    assert "Downloading openshift-install 5.0" in script
    assert "Dev-preview: EE image ships GA 4.x clients" in script
    assert "if true; then" in script
    assert "if ! command -v openshift-install" not in script


def test_ensure_installers_cmd_4x_replaces_baked_in_installer():
    script = _ensure_installers_cmd("4.20.12")
    assert "if ! command -v openshift-install" not in script
    assert "/4.20.12/openshift-install-linux.tar.gz" in script
    assert "dev-preview" not in script


def test_ensure_installers_cmd_okd_scos(monkeypatch):
    monkeypatch.setattr(cm, "resolve_okd_scos_tag", lambda v: "4.22.0-okd-scos.9")
    script = _ensure_installers_cmd("4.22", distribution="okd-scos")
    assert "github.com/okd-project/okd/releases/download/4.22.0-okd-scos.9/" in script
    assert "OKD/SCOS: EE image ships OCP clients" in script
    assert "if true; then" in script
