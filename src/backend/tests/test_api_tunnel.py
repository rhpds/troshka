"""Tests for nested OCP API tunnel helpers and cluster listing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from app.services.ocp.api_tunnel import (
    ClusterAccess,
    DialTarget,
    _ExecRelaySocket,
    get_cluster_access,
    list_cluster_access,
    localhost_kubeconfig,
    open_kubevirt_exec_relay,
    resolve_dial_targets,
)


def _project(topo: dict):
    return SimpleNamespace(
        id="e0f60a08-9aa0-4f9b-b2f4-686fa5517e9b",
        deployed_topology=topo,
        topology=None,
    )


def _topo_two_clusters() -> dict:
    return {
        "clusters": [
            {
                "id": "source-cae33a",
                "name": "source",
                "apiVip": "10.0.0.10",
                "baseDomain": "source.cclm.local",
            },
            {
                "id": "destination-8fb943",
                "name": "destination",
                "apiVip": "10.0.0.110",
                "baseDomain": "dest.cclm.local",
            },
        ],
        "nodes": [
            {
                "type": "vmNode",
                "id": "n1",
                "data": {
                    "name": "source-cp-0",
                    "clusterId": "source-cae33a",
                    "clusterRole": "controlplane",
                    "ocpKubeconfig": yaml.safe_dump(
                        {
                            "apiVersion": "v1",
                            "kind": "Config",
                            "clusters": [
                                {
                                    "name": "source",
                                    "cluster": {
                                        "server": "https://api.source.source.cclm.local:6443",
                                        "insecure-skip-tls-verify": True,
                                    },
                                }
                            ],
                            "contexts": [
                                {
                                    "name": "admin",
                                    "context": {"cluster": "source", "user": "admin"},
                                }
                            ],
                            "users": [{"name": "admin", "user": {"token": "t"}}],
                            "current-context": "admin",
                        }
                    ),
                },
            },
            {
                "type": "vmNode",
                "id": "n2",
                "data": {
                    "name": "dest-cp-0",
                    "clusterId": "destination-8fb943",
                    "clusterRole": "controlplane",
                    "ocpKubeconfig": yaml.safe_dump(
                        {
                            "apiVersion": "v1",
                            "kind": "Config",
                            "clusters": [
                                {
                                    "name": "destination",
                                    "cluster": {
                                        "server": "https://api.destination.dest.cclm.local:6443",
                                        "insecure-skip-tls-verify": True,
                                    },
                                }
                            ],
                            "contexts": [
                                {
                                    "name": "admin",
                                    "context": {
                                        "cluster": "destination",
                                        "user": "admin",
                                    },
                                }
                            ],
                            "users": [{"name": "admin", "user": {"token": "t"}}],
                            "current-context": "admin",
                        }
                    ),
                },
            },
            {
                "type": "networkNode",
                "id": "gw",
                "data": {
                    "subtype": "gateway",
                    "externalEndpoints": [
                        {
                            "port": 6443,
                            "type": "route",
                            "vmIp": "10.0.0.10",
                            "vmName": "source-cp-0",
                            "hostname": "rt-source-cp-0-6443.apps.example.com",
                        },
                        {
                            "port": 6444,
                            "type": "route",
                            "vmIp": "10.0.0.110",
                            "vmName": "dest-cp-0",
                            "hostname": "rt-dest-cp-0-6443.apps.example.com",
                        },
                    ],
                },
            },
        ],
    }


def test_list_cluster_access_includes_route_and_kubeconfig():
    rows = list_cluster_access(_project(_topo_two_clusters()))
    assert [r.name for r in rows] == ["source", "destination"]
    src = rows[0]
    assert src.kubeconfig_available
    assert src.api_vip == "10.0.0.10"
    assert src.tls_server_name == "api.source.source.cclm.local"
    assert src.route_hostname == "rt-source-cp-0-6443.apps.example.com"


def test_get_cluster_access_by_name_or_id():
    p = _project(_topo_two_clusters())
    assert get_cluster_access(p, "source").id == "source-cae33a"
    assert get_cluster_access(p, "destination-8fb943").name == "destination"
    assert get_cluster_access(p, "missing") is None


def test_localhost_kubeconfig_sets_port_and_sni():
    raw = _topo_two_clusters()["nodes"][0]["data"]["ocpKubeconfig"]
    out = yaml.safe_load(
        localhost_kubeconfig(
            raw,
            18443,
            tls_server_name="rt-source.apps.example.com",
            force_insecure=True,
        )
    )
    cl = out["clusters"][0]["cluster"]
    assert cl["server"] == "https://127.0.0.1:18443"
    assert cl["tls-server-name"] == "rt-source.apps.example.com"
    assert cl["insecure-skip-tls-verify"] is True


def test_resolve_dial_targets_troshkad_when_no_provider():
    p = _project(_topo_two_clusters())
    cluster = get_cluster_access(p, "source")
    host = SimpleNamespace(provider_id=None, host_type="shared")
    db = MagicMock()
    targets = resolve_dial_targets(p, cluster, host, db)
    assert targets == [
        DialTarget(
            host="10.0.0.10",
            port=6443,
            via="troshkad",
            tls_server_name="api.source.source.cclm.local",
            force_insecure=False,
        )
    ]


def test_resolve_dial_targets_ocpvirt_uses_troshkad():
    """ocpvirt = nested virt + troshkad; not in-cluster KubeVirt PF."""
    p = _project(_topo_two_clusters())
    cluster = get_cluster_access(p, "source")
    host = SimpleNamespace(provider_id="prov-1", host_type="shared")
    provider = SimpleNamespace(id="prov-1", type="ocpvirt")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = provider
    targets = resolve_dial_targets(p, cluster, host, db)
    assert targets == [
        DialTarget(
            host="10.0.0.10",
            port=6443,
            via="troshkad",
            tls_server_name="api.source.source.cclm.local",
            force_insecure=False,
        )
    ]


def test_resolve_dial_targets_kubevirt_prefers_portforward_then_exec():
    p = _project(_topo_two_clusters())
    cluster = get_cluster_access(p, "source")
    host = SimpleNamespace(provider_id="prov-1", host_type="kubevirt-cluster")
    provider = SimpleNamespace(id="prov-1", type="kubevirt")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = provider

    svc = MagicMock()
    svc.spec.cluster_ip = "172.30.1.50"
    svc.spec.ports = [SimpleNamespace(port=6443)]
    pod = MagicMock()
    pod.metadata.name = "gateway-troshka-e0f60a08-abc"
    pod.status.phase = "Running"
    core = MagicMock()
    core.read_namespaced_service.return_value = svc
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])

    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), core, MagicMock()),
        ),
        patch(
            "app.services.providers.kubevirt._project_ns",
            return_value="troshka-e0f60a08",
        ),
    ):
        targets = resolve_dial_targets(p, cluster, host, db)

    assert targets[0].via == "kubevirt-pf"
    assert targets[0].pod_name == "gateway-troshka-e0f60a08-abc"
    assert targets[0].port == 6443
    assert targets[1].via == "kubevirt-exec"
    assert targets[1].api_vip == "10.0.0.10"
    assert targets[0].tls_server_name == "api.source.source.cclm.local"
    assert any(t.host == "172.30.1.50" for t in targets)
    assert targets[-1].host == "rt-source-cp-0-6443.apps.example.com"
    assert targets[-1].force_insecure is True


def test_resolve_dial_targets_kubevirt_skips_pf_without_api_route():
    """Showroom-only gateways listen on 1443; do not PF to closed :6443."""
    topo = {
        "clusters": [
            {
                "id": "ocp-8a2eb4",
                "name": "ocp",
                "apiVip": "10.0.0.10",
                "baseDomain": "local",
            }
        ],
        "nodes": [
            {
                "type": "vmNode",
                "id": "cp",
                "data": {
                    "name": "cp-0",
                    "clusterId": "ocp-8a2eb4",
                    "clusterRole": "controlplane",
                    "ocpKubeconfig": "apiVersion: v1\nkind: Config\n",
                },
            },
            {
                "type": "networkNode",
                "id": "gw",
                "data": {
                    "subtype": "gateway",
                    "externalEndpoints": [
                        {
                            "port": 443,
                            "type": "route",
                            "vmIp": "172.30.232.3",
                            "vmName": "showroom",
                            "hostname": "showroom.apps.example.com",
                        }
                    ],
                },
            },
        ],
    }
    p = _project(topo)
    cluster = get_cluster_access(p, "ocp")
    host = SimpleNamespace(provider_id="prov-1", host_type="kubevirt-cluster")
    provider = SimpleNamespace(id="prov-1", type="kubevirt")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = provider
    pod = MagicMock()
    pod.metadata.name = "gateway-troshka-320fe5fb-abc"
    pod.status.phase = "Running"
    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
    core.read_namespaced_service.side_effect = Exception("no api svc")

    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), core, MagicMock()),
        ),
        patch(
            "app.services.providers.kubevirt._project_ns",
            return_value="troshka-320fe5fb",
        ),
    ):
        targets = resolve_dial_targets(p, cluster, host, db)

    assert [t.via for t in targets] == ["kubevirt-exec"]
    assert targets[0].api_vip == "10.0.0.10"
    assert targets[0].pod_name == "gateway-troshka-320fe5fb-abc"


def test_open_kubevirt_exec_relay_uses_binary_stream():
    """TLS-over-socat requires kubernetes stream(binary=True) or stdout is UTF-8 mangled."""
    provider = SimpleNamespace()
    target = DialTarget(
        host="gw",
        port=6443,
        via="kubevirt-exec",
        tls_server_name="api.ocp.local",
        force_insecure=False,
        namespace="troshka-abc",
        pod_name="gateway-1",
        api_vip="10.0.0.10",
    )
    core = MagicMock()
    captured = {}

    def _stream(method, *args, **kwargs):
        captured.update(kwargs)
        return MagicMock(name="ws")

    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), core, MagicMock()),
        ),
        patch("kubernetes.stream.stream", side_effect=_stream) as mock_stream,
    ):
        sock = open_kubevirt_exec_relay(provider, target)

    assert isinstance(sock, _ExecRelaySocket)
    assert captured.get("binary") is True
    assert captured.get("_preload_content") is False
    mock_stream.assert_called_once()


def test_exec_relay_socket_preserves_binary_tls_bytes():
    ws = MagicMock()
    ws.is_open.return_value = True
    # Invalid-as-UTF-8 TLS-like payload must round-trip unchanged.
    payload = bytes(range(256)) + b"\x16\x03\x01\x00\x00"
    ws.read_stdout.return_value = payload
    sock = _ExecRelaySocket(ws)
    got = sock.recv(65536)
    assert got == payload
    sock.sendall(b"\x16\x03\x01\x00\x05hello")
    ws.write_stdin.assert_called_once_with(b"\x16\x03\x01\x00\x05hello")


def test_exec_relay_socket_buffers_partial_recv():
    """Truncating a large WS frame without buffering drops TLS bytes (bad MAC)."""
    ws = MagicMock()
    ws.is_open.return_value = True
    ws.read_stdout.return_value = b"ABCDEFGHIJ"
    sock = _ExecRelaySocket(ws)
    assert sock.recv(4) == b"ABCD"
    # Second recv must return buffered remainder without another WS read.
    ws.read_stdout.return_value = b""
    assert sock.recv(10) == b"EFGHIJ"
    assert ws.read_stdout.call_count == 1


def test_cluster_access_dataclass_fields():
    row = ClusterAccess(
        id="c1",
        name="ocp",
        api_vip="10.0.0.10",
        base_domain="lab.local",
        kubeconfig="x",
        kubeconfig_available=True,
        tls_server_name="api.ocp.lab.local",
        route_hostname="",
    )
    assert row.name == "ocp"
