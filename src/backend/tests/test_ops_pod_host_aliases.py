from app.services.ocp.ops_pod_scaffold import (
    build_ops_pod_kubevirt_manifests,
    kubevirt_cluster_api_host_aliases,
    lab_pod_dns_config,
)


def test_kubevirt_cluster_api_host_aliases_multi_cluster():
    topo = {
        "clusters": [
            {
                "id": "source",
                "name": "source",
                "base_domain": "source.cclm.local",
                "apiVip": "10.0.0.10",
            },
            {
                "id": "destination",
                "name": "destination",
                "baseDomain": "dest.cclm.local",
                "api_vip": "10.0.0.110",
            },
        ]
    }
    aliases = kubevirt_cluster_api_host_aliases(topo)
    assert aliases == [
        {
            "ip": "10.0.0.10",
            "hostnames": [
                "api-int.source.source.cclm.local",
                "api.source.source.cclm.local",
            ],
        },
        {
            "ip": "10.0.0.110",
            "hostnames": [
                "api-int.destination.dest.cclm.local",
                "api.destination.dest.cclm.local",
            ],
        },
    ]


def test_build_ops_pod_manifest_includes_host_aliases():
    pod, _secret = build_ops_pod_kubevirt_manifests(
        namespace="ns",
        project_id="p1234567890",
        command=["sleep"],
        env={},
        config_files={},
        cluster_nads=[],
        bmc_nad=None,
        host_aliases=[
            {
                "ip": "10.0.0.10",
                "hostnames": ["api.source.source.cclm.local"],
            }
        ],
    )
    assert pod["spec"]["hostAliases"][0]["ip"] == "10.0.0.10"


def test_lab_pod_dns_config_forces_tcp():
    assert lab_pod_dns_config("10.0.0.2") == {
        "nameservers": ["10.0.0.2"],
        "options": [{"name": "use-vc"}],
    }


def test_build_ops_pod_mounts_project_ceph_secret():
    pod, _secret = build_ops_pod_kubevirt_manifests(
        namespace="ns",
        project_id="p1234567890",
        command=["sleep"],
        env={},
        config_files={},
        cluster_nads=[],
        bmc_nad=None,
        mount_project_ceph_secret=True,
    )
    mounts = pod["spec"]["containers"][0]["volumeMounts"]
    assert any(m["mountPath"] == "/workdir/project-ceph" for m in mounts)
    volumes = pod["spec"]["volumes"]
    assert any(v["name"] == "project-ceph" for v in volumes)


def test_build_ops_pod_manifest_dns_uses_tcp():
    pod, _secret = build_ops_pod_kubevirt_manifests(
        namespace="ns",
        project_id="p1234567890",
        command=["sleep"],
        env={},
        config_files={},
        cluster_nads=[],
        bmc_nad=None,
        dns_nameserver="10.0.0.2",
    )
    assert pod["spec"]["dnsConfig"] == lab_pod_dns_config("10.0.0.2")
