"""Deletion must preserve authorization until network finalization completes."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from kubernetes.client.exceptions import ApiException

from app.services.providers import kubevirt


@pytest.fixture
def destroy_clients(monkeypatch):
    custom, core = Mock(), Mock()
    monkeypatch.setattr(kubevirt, "_get_k8s_clients", lambda _: (custom, core, Mock()))
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *_: "troshka-project")
    monkeypatch.setattr(kubevirt.time, "sleep", lambda _: None)
    monkeypatch.setattr(kubevirt, "_PROJECT_CR_GONE_ATTEMPTS", 2)
    for name in (
        "_stop_vms_gracefully",
        "_wait_for_vmis_terminated",
        "_force_delete_vmis",
        "_force_delete_virt_launcher_pods",
        "_wait_for_virt_launchers_gone",
        "_cleanup_volume_attachments",
        "_delete_vm_crs",
        "_delete_namespace_jobs",
        "_cleanup_pattern_capture_temps",
        "_delete_namespaced_services",
    ):
        monkeypatch.setattr(kubevirt, name, Mock())
    monkeypatch.setattr(kubevirt, "_wait_for_project_cr_gone", lambda *_: True)
    monkeypatch.setattr(kubevirt, "_wait_for_ns_services_gone", lambda *_: True)
    core.read_namespace.return_value = SimpleNamespace(
        metadata=SimpleNamespace(deletion_timestamp=None)
    )
    return custom, core


def test_namespace_delete_waits_for_network_finalization(destroy_clients):
    custom, core = destroy_clients
    pending = [{"metadata": {"name": "net-one", "deletionTimestamp": "now"}}]
    states = iter([pending, []])
    observed = []

    def list_networks(**kwargs):
        assert kwargs["plural"] == "troshkanetworks"
        observed[:] = next(states)
        return {"items": observed.copy()}

    custom.list_namespaced_custom_object.side_effect = list_networks
    core.delete_namespace.side_effect = lambda **_: (
        None if not observed else pytest.fail("Namespace deleted over live finalizer")
    )
    kubevirt.KubeVirtDriver().destroy_project(Mock(), "project")
    assert custom.list_namespaced_custom_object.call_count == 2
    core.delete_namespace.assert_called_once()


@pytest.mark.parametrize("failure", [None, 403, 500])
def test_network_blocker_preserves_namespace_permissions(destroy_clients, failure):
    custom, core = destroy_clients
    custom.list_namespaced_custom_object.return_value = {
        "items": [{"metadata": {"name": "net-one", "deletionTimestamp": "now"}}]
    }
    if failure:
        custom.list_namespaced_custom_object.side_effect = ApiException(status=failure)
    with pytest.raises((RuntimeError, ApiException)):
        kubevirt.KubeVirtDriver().destroy_project(Mock(), "project")
    core.delete_namespace.assert_not_called()


def test_terminating_namespace_proceeds_to_existing_recovery(destroy_clients):
    custom, core = destroy_clients
    core.read_namespace.return_value.metadata.deletion_timestamp = "now"
    custom.list_namespaced_custom_object.side_effect = ApiException(status=403)
    kubevirt.KubeVirtDriver().destroy_project(Mock(), "project")
    custom.delete_namespaced_custom_object.assert_not_called()
    core.delete_namespace.assert_not_called()


@pytest.mark.parametrize("status", [404, 403, 500])
def test_namespace_lookup_only_treats_not_found_as_deleted(destroy_clients, status):
    custom, core = destroy_clients
    core.read_namespace.side_effect = ApiException(status=status)
    if status == 404:
        kubevirt.KubeVirtDriver().destroy_project(Mock(), "project")
    else:
        with pytest.raises(ApiException):
            kubevirt.KubeVirtDriver().destroy_project(Mock(), "project")
    custom.delete_namespaced_custom_object.assert_not_called()
    core.delete_namespace.assert_not_called()


def _cluster_permissions():
    manifest = Path(__file__).resolve().parents[3] / "infra/ocpvirt-rbac.yaml"
    docs = list(yaml.safe_load_all(manifest.read_text()))
    roles = {
        d["metadata"]["name"]: d["rules"] for d in docs if d["kind"] == "ClusterRole"
    }
    return [
        rule
        for doc in docs
        if doc["kind"] == "ClusterRoleBinding"
        and {"kind": "ServiceAccount", "name": "troshka", "namespace": "troshka"}
        in doc["subjects"]
        for rule in roles[doc["roleRef"]["name"]]
    ]


@pytest.mark.parametrize(
    "group,resource,verb",
    [
        ("", resource, "list")
        for resource in ("pods", "persistentvolumeclaims", "configmaps", "services")
    ]
    + [
        ("apps", "deployments", "list"),
        ("troshka.redhat.com", "troshkaprojects", "list"),
        ("troshka.redhat.com", "troshkanetworks", "list"),
        ("troshka.redhat.com", "troshkanetworks", "patch"),
        ("kubevirt.io", "virtualmachines", "list"),
        ("kubevirt.io", "virtualmachineinstances", "list"),
        ("cdi.kubevirt.io", "datavolumes", "list"),
        ("k8s.cni.cncf.io", "network-attachment-definitions", "list"),
    ],
)
def test_network_recovery_authorized_without_namespace_bindings(group, resource, verb):
    assert any(
        group in rule["apiGroups"]
        and resource in rule["resources"]
        and verb in rule["verbs"]
        and not rule.get("resourceNames")
        for rule in _cluster_permissions()
    ), f"Recovery loses {verb} {group}/{resource} when RoleBindings disappear"


def test_recovery_does_not_grant_general_namespaced_mutation():
    for rule in _cluster_permissions():
        if set(rule["resources"]) & {
            "pods",
            "persistentvolumeclaims",
            "configmaps",
            "deployments",
            "virtualmachines",
            "secrets",
        }:
            assert set(rule["verbs"]) <= {"get", "list", "watch"}
