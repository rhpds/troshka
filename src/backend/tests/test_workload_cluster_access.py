from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.workloads import cluster_access

_KUBECONFIG = """
apiVersion: v1
clusters:
- cluster: {server: https://api.cl.example.com:6443}
  name: cl
contexts:
- context: {cluster: cl, user: admin}
  name: admin
current-context: admin
users:
- name: admin
  user: {token: bootstrap-tok}
"""


def test_mint_admin_token_calls_tokenrequest(monkeypatch):
    fake_core = MagicMock()
    fake_core.create_namespaced_service_account_token.return_value = SimpleNamespace(
        status=SimpleNamespace(token="minted-sa-token")
    )
    monkeypatch.setattr(
        cluster_access,
        "_core_v1_from_kubeconfig",
        lambda kc: (fake_core, "https://api.cl.example.com:6443"),
    )
    monkeypatch.setattr(cluster_access, "_ensure_admin_sa", lambda core, sa, ns: None)

    api_url, token = cluster_access._mint_admin_token(_KUBECONFIG)
    assert api_url == "https://api.cl.example.com:6443"
    assert token == "minted-sa-token"
    assert fake_core.create_namespaced_service_account_token.called


def test_resolve_cluster_access_maps_by_cluster_name(monkeypatch):
    project = SimpleNamespace(
        deployed_topology={"nodes": []},
        topology={"nodes": []},
    )
    monkeypatch.setattr(
        cluster_access,
        "_stored_cluster_creds",
        lambda topo: {"cl-1": ("pw", _KUBECONFIG)},
    )
    monkeypatch.setattr(
        cluster_access,
        "_mint_admin_token",
        lambda kc, **kw: ("https://api.cl.example.com:6443", "tok-1"),
    )
    out = cluster_access.resolve_cluster_access(project)
    assert out["cl-1"]["api_url"].endswith(":6443")
    assert out["cl-1"]["api_token"] == "tok-1"


def test_resolve_cluster_access_no_clusters_raises(monkeypatch):
    project = SimpleNamespace(deployed_topology=None, topology={"nodes": []})
    monkeypatch.setattr(cluster_access, "_stored_cluster_creds", lambda topo: {})
    with pytest.raises(cluster_access.ClusterAccessError):
        cluster_access.resolve_cluster_access(project)
