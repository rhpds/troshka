import sys
import os

# Add operator source to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock kopf before any handler imports
from unittest.mock import MagicMock

import pytest

sys.modules["kopf"] = MagicMock()


@pytest.fixture
def legacy_producer_context(monkeypatch):
    """Legacy inner-handler tests receive an explicit nonquota producer root.

    Real UID lookup, serialization and stopped-intent behavior are covered by
    test_managed_power. Keep ambient credential/API denial active in these tests.
    """
    from helpers import managed_power

    root = {"kind": "TroshkaProject", "metadata": {"name": "legacy", "uid": "legacy-uid"}}
    from types import SimpleNamespace

    monkeypatch.setattr(managed_power, "PowerClient", lambda: SimpleNamespace(get=lambda *args: {"metadata": {}}))
    monkeypatch.setattr(managed_power, "root_for", lambda *args: root)


@pytest.fixture(autouse=True)
def _deny_real_kubernetes_access(monkeypatch):
    """Unit tests never load ambient credentials or invoke real cluster APIs.

    Teardown detects even attempts swallowed by broad production error handling.
    Tests may replace these boundaries with their own synthetic mocks.
    """
    boundaries = []
    for path in (
        "kubernetes.client.ApiClient.call_api",
        "kubernetes.config.load_kube_config",
        "kubernetes.config.load_incluster_config",
    ):
        boundary = MagicMock(side_effect=AssertionError("unit_test_cluster_access"))
        monkeypatch.setattr(path, boundary)
        boundaries.append(boundary)
    yield
    for boundary in boundaries:
        boundary.assert_not_called()
