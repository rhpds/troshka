"""Tests for merging per-cluster kubeconfigs into one for the showroom oc
terminal (contexts renamed to the cluster display name; single cluster is the
default context so it 'just works')."""

from __future__ import annotations

import yaml

from app.services.ocp.kubeconfig_merge import merge_kubeconfigs


def _kc(
    cluster="admin-cluster", user="admin-user", ctx="admin", server="https://api.x:6443"
) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": cluster,
                    "cluster": {"server": server, "certificate-authority-data": "Q0E="},
                }
            ],
            "users": [{"name": user, "user": {"token": "sha256~abc"}}],
            "contexts": [
                {
                    "name": ctx,
                    "context": {
                        "cluster": cluster,
                        "user": user,
                        "namespace": "default",
                    },
                }
            ],
            "current-context": ctx,
        }
    )


def test_single_cluster_becomes_default_context():
    merged = yaml.safe_load(
        merge_kubeconfigs([("ocp", _kc(server="https://api.ocp:6443"))])
    )
    assert merged["current-context"] == "ocp"
    assert [c["name"] for c in merged["contexts"]] == ["ocp"]
    assert merged["contexts"][0]["context"]["cluster"] == "ocp"
    assert merged["contexts"][0]["context"]["user"] == "ocp"
    assert merged["contexts"][0]["context"]["namespace"] == "default"
    # cluster + user renamed to the display name, bodies preserved
    assert merged["clusters"][0] == {
        "name": "ocp",
        "cluster": {
            "server": "https://api.ocp:6443",
            "certificate-authority-data": "Q0E=",
        },
    }
    assert merged["users"][0]["user"] == {"token": "sha256~abc"}


def test_multi_cluster_one_context_each_first_is_default():
    merged = yaml.safe_load(
        merge_kubeconfigs(
            [
                ("prod", _kc(server="https://api.prod:6443")),
                ("edge", _kc(server="https://api.edge:6443")),
            ]
        )
    )
    assert merged["current-context"] == "prod"
    assert sorted(c["name"] for c in merged["contexts"]) == ["edge", "prod"]
    servers = {c["name"]: c["cluster"]["server"] for c in merged["clusters"]}
    assert servers == {"prod": "https://api.prod:6443", "edge": "https://api.edge:6443"}


def test_skips_empty_and_malformed():
    merged = yaml.safe_load(
        merge_kubeconfigs([("a", ""), ("b", "::not yaml::"), ("ocp", _kc())])
    )
    assert [c["name"] for c in merged["contexts"]] == ["ocp"]


def test_no_configs_yields_empty_valid_config():
    merged = yaml.safe_load(merge_kubeconfigs([]))
    assert merged["kind"] == "Config"
    assert merged["contexts"] == []
    assert merged["current-context"] == ""


def test_display_name_sanitized_for_context():
    merged = yaml.safe_load(merge_kubeconfigs([("My Cluster", _kc())]))
    # spaces are not shell/oc-friendly in a context name
    assert merged["current-context"] == "my-cluster"
