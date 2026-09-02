"""Live-env harness: sys.path bootstrap, collection skip-guard, fixtures.

Fixtures are added in Task 4; this task establishes import bootstrap + skips.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))  # make live_* modules importable

import pytest  # noqa: E402
from live_config import LiveConfig  # noqa: E402


def pytest_configure(config):
    for line in (
        "live_env: requires a running Troshka instance (TROSHKA_LIVE_URL)",
        "live_troshkad: requires a troshkad/libvirt host",
        "live_kubevirt: requires a kubevirt-cluster host",
        "tier2: slow real OCP install (~30-60 min)",
    ):
        config.addinivalue_line("markers", line)


def pytest_collection_modifyitems(config, items):
    cfg = LiveConfig.from_env()
    for item in items:
        if "live_env" not in item.keywords:
            continue
        if not cfg.configured:
            item.add_marker(pytest.mark.skip(reason="TROSHKA_LIVE_URL not set"))
            continue
        if "live_troshkad" in item.keywords and not cfg.troshkad_ready:
            item.add_marker(
                pytest.mark.skip(reason="TROSHKA_LIVE_TROSHKAD_HOST not set")
            )
        if "live_kubevirt" in item.keywords and not cfg.kubevirt_ready:
            item.add_marker(
                pytest.mark.skip(reason="kubevirt env (KUBECONFIG+HOST) not set")
            )
        if "tier2" in item.keywords and not cfg.tier2_enabled:
            item.add_marker(pytest.mark.skip(reason="TROSHKA_LIVE_TIER2 != 1"))


from live_api import LiveClient  # noqa: E402


@pytest.fixture(scope="session")
def live_config():
    return LiveConfig.from_env()


@pytest.fixture(scope="session")
def client(live_config):
    if not live_config.configured:
        pytest.skip("TROSHKA_LIVE_URL not set")
    c = LiveClient(live_config.url, token=live_config.token)
    yield c
    c.close()


@pytest.fixture(scope="session")
def ocp_template(client):
    templates = client.get_json("/api/v1/projects/templates")
    ids = [t["id"] for t in templates if t.get("category") == "openshift"]
    assert "ocp-sno" in ids, f"ocp-sno template missing; found {ids}"
    return "ocp-sno"


@pytest.fixture
def project_factory(client):
    created: list[str] = []

    def create(**body):
        payload = {"auto_install_ocp": True, "install_via": "pod", **body}
        r = client.post_json("/api/v1/projects/from-template", payload)
        assert r.status_code == 201, f"from-template failed: {r.status_code} {r.text}"
        pid = r.json()["id"]
        created.append(pid)
        return pid

    yield create
    for pid in created:
        try:
            client.delete(f"/api/v1/projects/{pid}")
        except Exception:
            pass
