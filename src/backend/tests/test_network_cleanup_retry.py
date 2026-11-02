from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException

from app.services.providers import kubevirt


@pytest.fixture
def clients(monkeypatch):
    custom, core, apps = Mock(), Mock(), Mock()
    monkeypatch.setattr("kubernetes.client.AppsV1Api", lambda _: apps)
    monkeypatch.setattr(kubevirt, "_get_k8s_clients", lambda _: (custom, core, apps))
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *_: "troshka-project")
    core.read_namespace.return_value.metadata.deletion_timestamp = "now"
    for method in (
        "list_namespaced_pod",
        "list_namespaced_persistent_volume_claim",
        "list_namespaced_config_map",
        "list_namespaced_service",
    ):
        getattr(core, method).return_value = SimpleNamespace(items=[])
    apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[])
    network = {
        "metadata": {
            "name": "net-one",
            "resourceVersion": "42",
            "deletionTimestamp": "now",
            "finalizers": ["troshka.redhat.com/finalizer", "other"],
        }
    }
    custom.list_namespaced_custom_object.side_effect = lambda **kw: {
        "items": [network] if kw["plural"] == "troshkanetworks" else []
    }
    custom.get_cluster_custom_object.return_value = {
        "metadata": {"resourceVersion": "7"},
        "users": ["unrelated", "system:serviceaccount:troshka-project:troshka-network"],
    }
    return custom, core, apps, network


def test_recovers_only_owned_finalizer_after_scc_cleanup(clients):
    custom, _, _, _ = clients
    assert kubevirt._retry_network_cleanup(Mock(), "project") is True
    patch = custom.patch_namespaced_custom_object.call_args.kwargs
    assert patch["body"] == {
        "metadata": {"resourceVersion": "42", "finalizers": ["other"]}
    }
    assert custom.patch_cluster_custom_object.call_count == 2
    for call in custom.patch_cluster_custom_object.call_args_list:
        assert call.kwargs["body"]["users"] == ["unrelated"]
        assert call.kwargs["body"]["metadata"]["resourceVersion"] == "7"


@pytest.mark.parametrize(
    "dependency",
    ["pod", "persistent_volume_claim", "config_map", "service", "deployment"],
)
def test_live_dependencies_prevent_release(clients, dependency):
    custom, core, apps, _ = clients
    api = apps if dependency == "deployment" else core
    getattr(api, "list_namespaced_" + dependency).return_value.items = [Mock()]
    assert kubevirt._retry_network_cleanup(Mock(), "project") is False
    custom.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.parametrize(
    "plural",
    [
        "virtualmachines",
        "virtualmachineinstances",
        "datavolumes",
        "network-attachment-definitions",
        "troshkaprojects",
    ],
)
def test_custom_dependencies_prevent_release(clients, plural):
    custom, _, _, network = clients
    custom.list_namespaced_custom_object.side_effect = lambda **kw: {
        "items": [network] if kw["plural"] in (plural, "troshkanetworks") else []
    }
    assert kubevirt._retry_network_cleanup(Mock(), "project") is False
    custom.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.parametrize("target", ["namespace", "network"])
def test_active_resources_are_untouched(clients, target):
    custom, core, _, network = clients
    if target == "namespace":
        core.read_namespace.return_value.metadata.deletion_timestamp = None
    else:
        network["metadata"].pop("deletionTimestamp")
    assert kubevirt._retry_network_cleanup(Mock(), "project") is False
    custom.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.parametrize("status", [403, 409, 500])
def test_scc_failure_retains_finalizer(clients, status):
    custom, _, _, _ = clients
    custom.patch_cluster_custom_object.side_effect = ApiException(status=status)
    with pytest.raises(ApiException):
        kubevirt._retry_network_cleanup(Mock(), "project")
    custom.patch_namespaced_custom_object.assert_not_called()


def test_dependency_lookup_failure_retains_finalizer(clients):
    custom, core, _, _ = clients
    core.list_namespaced_pod.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        kubevirt._retry_network_cleanup(Mock(), "project")
    custom.patch_namespaced_custom_object.assert_not_called()
    custom.patch_cluster_custom_object.assert_not_called()


def test_missing_scc_is_already_clean(clients):
    custom, _, _, _ = clients
    custom.get_cluster_custom_object.side_effect = ApiException(status=404)
    assert kubevirt._retry_network_cleanup(Mock(), "project") is True
    custom.patch_cluster_custom_object.assert_not_called()
