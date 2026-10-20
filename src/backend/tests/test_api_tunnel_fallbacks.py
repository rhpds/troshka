"""Candidate ordering and partial-discovery failures for nested API access."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.ocp.api_tunnel import (
    ClusterAccess,
    _cp_kubeconfig,
    _kubevirt_dial_targets,
)


@pytest.fixture
def discovery():
    cluster = ClusterAccess(
        "c", "cluster", "10.0.0.10", "lab", "", False, "api.lab", "route.lab"
    )
    topology = {
        "nodes": [
            {
                "data": {
                    "subtype": "gateway",
                    "externalEndpoints": [
                        {
                            "vmIp": "10.0.0.10",
                            "vmName": "cp-0",
                            "type": "route",
                            "port": 6444,
                        }
                    ],
                }
            }
        ]
    }
    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(name="gateway"),
                status=SimpleNamespace(phase="Running"),
            )
        ]
    )
    core.read_namespaced_service.return_value = SimpleNamespace(
        spec=SimpleNamespace(
            cluster_ip="172.30.0.10", ports=[SimpleNamespace(port=6445)]
        )
    )
    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(None, core, None),
        ) as clients,
        patch("app.services.providers.kubevirt._project_ns", return_value="project-ns"),
    ):
        yield cluster, topology, core, clients


def test_candidate_order_ports_and_tls_policy(discovery):
    cluster, topology, core, _ = discovery
    targets = _kubevirt_dial_targets(None, "project-id", cluster, topology)
    assert [(t.via, t.host, t.port) for t in targets] == [
        ("kubevirt-pf", "gateway", 6444),
        ("kubevirt-exec", "gateway", 6443),
        ("direct", "172.30.0.10", 6445),
        ("direct", "route.lab", 443),
    ]
    assert [(t.tls_server_name, t.force_insecure) for t in targets] == [
        ("api.lab", False),
        ("api.lab", False),
        ("api.lab", False),
        ("route.lab", True),
    ]
    core.read_namespaced_service.assert_called_once_with("rt-cp-0-6443", "project-ns")


@pytest.mark.parametrize("failure", ["clients", "pods", "service"])
def test_lookup_failures_preserve_available_fallbacks(discovery, failure):
    cluster, topology, core, clients = discovery
    if failure == "clients":
        clients.side_effect = RuntimeError("unavailable")
        expected = ["route.lab"]
    elif failure == "pods":
        core.list_namespaced_pod.side_effect = RuntimeError("forbidden")
        expected = ["172.30.0.10", "route.lab"]
    else:
        core.read_namespaced_service.side_effect = RuntimeError("not found")
        expected = ["gateway", "gateway", "route.lab"]
    targets = _kubevirt_dial_targets(None, "project-id", cluster, topology)
    assert [t.host for t in targets] == expected


@pytest.mark.parametrize("cluster_ip", [None, "", "None", "none"])
def test_headless_or_missing_service_address_is_not_dialed(discovery, cluster_ip):
    cluster, topology, core, _ = discovery
    core.read_namespaced_service.return_value.spec.cluster_ip = cluster_ip
    targets = _kubevirt_dial_targets(None, "project-id", cluster, topology)
    assert [t.host for t in targets] == ["gateway", "gateway", "route.lab"]


@pytest.mark.parametrize("published", [True, False])
def test_missing_service_port_uses_published_port_or_api_default(discovery, published):
    cluster, topology, core, _ = discovery
    core.read_namespaced_service.return_value.spec.ports = []
    if not published:
        topology["nodes"][0]["data"]["externalEndpoints"][0]["type"] = "other"
    targets = _kubevirt_dial_targets(None, "project-id", cluster, topology)
    service = next(t for t in targets if t.host == "172.30.0.10")
    assert service.port == (6444 if published else 6443)
    assert any(t.via == "kubevirt-pf" for t in targets) is published


@pytest.mark.parametrize(
    "role,group,expected",
    [
        ("worker", "masters", "fallback"),
        ("", "workers", "fallback"),
        ("controlplane", "workers", "selected"),
        ("", "masters", "selected"),
    ],
)
def test_control_plane_kubeconfig_role_precedence(role, group, expected):
    topology = {
        "nodes": [
            {
                "type": "vmNode",
                "data": {"clusterId": "other", "ocpKubeconfig": "wrong cluster"},
            },
            {
                "type": "networkNode",
                "data": {"clusterId": "c", "ocpKubeconfig": "wrong type"},
            },
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "c",
                    "clusterRole": role,
                    "tags": {"AnsibleGroup": group},
                    "ocpKubeconfig": "selected",
                },
            },
            {"type": "vmNode", "data": {"clusterId": "c", "ocpKubeconfig": "fallback"}},
        ]
    }
    assert _cp_kubeconfig(topology, "c") == expected


@pytest.mark.parametrize("value", [None, "", "  ", 42])
def test_invalid_control_plane_kubeconfig_is_skipped(value):
    topology = {
        "nodes": [
            {"type": "vmNode", "data": {"clusterId": "c", "ocpKubeconfig": value}}
        ]
    }
    assert _cp_kubeconfig(topology, "c") == ""
