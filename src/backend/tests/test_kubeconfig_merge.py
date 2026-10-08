"""Tests for merging per-cluster kubeconfigs into one for the showroom oc
terminal (contexts renamed to the cluster display name; single cluster is the
default context so it 'just works')."""

from __future__ import annotations

import yaml

from app.services.ocp.kubeconfig_merge import (
    cluster_terminal_motd_text,
    is_valid_kubeadmin_password,
    is_valid_kubeconfig,
    kubeconfig_server_host,
    merge_kubeconfigs,
    rewrite_kubeconfig_server,
    rewrite_kubeconfig_server_to_host,
)


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


def test_is_valid_kubeadmin_password_rejects_cat_errors():
    assert not is_valid_kubeadmin_password(
        "cat: /workdir/destination/auth/kubeadmin-password: No such file"
    )
    assert is_valid_kubeadmin_password("raYLB-kf6JY-6naZk-XyCLH")


def test_is_valid_kubeconfig_rejects_cat_errors_and_accepts_real_config():
    assert not is_valid_kubeconfig(
        "cat: /workdir/destination/auth/kubeconfig: No such file or directory\n"
    )
    assert not is_valid_kubeconfig("")
    assert is_valid_kubeconfig(_kc())


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


def test_rewrite_to_host_sets_tls_server_name_like_showroom_terminal():
    """Showroom cluster terminal path: DNS API host → VIP + tls-server-name."""
    out = yaml.safe_load(
        rewrite_kubeconfig_server_to_host(
            _kc(server="https://api.source.source.cclm.local:6443"),
            "10.0.0.10",
        )
    )
    cl = out["clusters"][0]["cluster"]
    assert cl["server"] == "https://10.0.0.10:6443"
    assert cl["tls-server-name"] == "api.source.source.cclm.local"
    assert cl.get("certificate-authority-data") == "Q0E="


def test_rewrite_to_host_skips_already_ip_preserving_showroom_idempotence():
    raw = _kc(server="https://10.0.0.10:6443")
    assert rewrite_kubeconfig_server_to_host(raw, "10.0.0.110") == raw


def test_rewrite_to_host_preserves_insecure_skip_tls_verify():
    """Harvested kubeconfigs often set insecure-skip-tls-verify; keep it."""
    raw = yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "c",
                    "cluster": {
                        "server": "https://api.x.local:6443",
                        "insecure-skip-tls-verify": True,
                    },
                }
            ],
            "contexts": [{"name": "c", "context": {"cluster": "c", "user": "u"}}],
            "users": [{"name": "u", "user": {"token": "t"}}],
            "current-context": "c",
        }
    )
    out = yaml.safe_load(rewrite_kubeconfig_server_to_host(raw, "10.0.0.10"))
    cl = out["clusters"][0]["cluster"]
    assert cl["server"] == "https://10.0.0.10:6443"
    assert cl["tls-server-name"] == "api.x.local"
    assert cl["insecure-skip-tls-verify"] is True


def test_rewrite_localhost_tunnel_keeps_sni_and_sets_port():
    out = yaml.safe_load(
        rewrite_kubeconfig_server(
            _kc(server="https://api.x.local:6443"),
            "127.0.0.1",
            port=18443,
        )
    )
    cl = out["clusters"][0]["cluster"]
    assert cl["server"] == "https://127.0.0.1:18443"
    assert cl["tls-server-name"] == "api.x.local"


def test_rewrite_localhost_from_vip_preserves_existing_tls_server_name():
    vip = rewrite_kubeconfig_server_to_host(
        _kc(server="https://api.x.local:6443"), "10.0.0.10"
    )
    out = yaml.safe_load(rewrite_kubeconfig_server(vip, "127.0.0.1", port=19000))
    cl = out["clusters"][0]["cluster"]
    assert cl["server"] == "https://127.0.0.1:19000"
    assert cl["tls-server-name"] == "api.x.local"


def test_kubeconfig_server_host_extracts_hostname():
    assert (
        kubeconfig_server_host(_kc(server="https://api.x.local:6443")) == "api.x.local"
    )
    assert kubeconfig_server_host("") == ""


def test_deploy_service_wrapper_matches_shared_helper():
    """deploy_service._kubeconfig_server_to_ip must stay a thin alias."""
    from app.services.deploy_service import _kubeconfig_server_to_ip

    raw = _kc(server="https://api.ocp.local:6443")
    assert _kubeconfig_server_to_ip(
        raw, "10.1.0.10"
    ) == rewrite_kubeconfig_server_to_host(raw, "10.1.0.10")


def test_cluster_terminal_motd_includes_single_cluster_access_info():
    merged = merge_kubeconfigs([("source", _kc(server="https://10.0.0.10:6443"))])
    motd = cluster_terminal_motd_text(
        merged,
        clusters=[{"id": "source", "name": "source", "baseDomain": "source.lab.local"}],
        creds={"source": ("s3cr3t", _kc())},
    )
    assert "* source  (current)" in motd
    assert "API:       https://10.0.0.10:6443" in motd
    assert (
        "Console:   https://console-openshift-console.apps.source.source.lab.local"
        in motd
    )
    assert "Kubeadmin: s3cr3t" in motd


def test_cluster_terminal_motd_lists_per_context_details_for_multi_cluster():
    merged = merge_kubeconfigs(
        [
            ("source", _kc(server="https://10.0.0.10:6443")),
            ("destination", _kc(server="https://10.0.0.110:6443")),
        ]
    )
    motd = cluster_terminal_motd_text(
        merged,
        clusters=[
            {"id": "source", "name": "source", "baseDomain": "source.lab.local"},
            {
                "id": "destination",
                "name": "destination",
                "baseDomain": "dest.lab.local",
            },
        ],
        creds={
            "source": ("pw-source", _kc()),
            "destination": ("pw-dest", _kc()),
        },
    )
    assert "oc config get-contexts" in motd
    assert "oc config use-context <name>" in motd
    assert "* source  (current)" in motd
    assert "destination" in motd
    assert "Kubeadmin: pw-source" in motd
    assert "Kubeadmin: pw-dest" in motd
    assert "console-openshift-console.apps.destination.dest.lab.local" in motd
