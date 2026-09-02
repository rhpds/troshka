"""Tests for the ops-pod network + spec builder (Plan 4, Task 4).

Pure payload-shaping tests — no live pod is created here. The shaped dict is
consumed later by `_pod_create_params`-style logic (Task 5) to actually create
the in-cluster ops pod.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.deploy_topology import showroom_infra_network
from app.services.ocp.ops_pod_scaffold import (
    OPS_POD_IMAGE,
    build_ops_pod_config,
    ops_pod_infra_network,
)


def _vni_map() -> dict:
    # min VNI -> octet3 == 0x0A == 10
    return {"net-a": 4106, "net-b": 4200}


def test_ops_pod_infra_network_is_infra_transit_with_distinct_ip():
    nets = ops_pod_infra_network(_vni_map(), mac="52:54:00:aa:bb:cc")
    assert len(nets) == 1
    net = nets[0]
    assert net["infra_transit"] is True
    # Distinct pod IP (.4) so it coexists with the showroom pod (.3).
    assert net["ip"].endswith(".4")
    assert net["ip"] == "172.30.10.4"
    assert net["cidr"] == "172.30.10.0/24"
    assert net["gateway"] == "172.30.10.2"
    assert net["mac"] == "52:54:00:aa:bb:cc"


def test_ops_pod_ip_distinct_from_showroom_ip():
    ops = ops_pod_infra_network(_vni_map())
    show = showroom_infra_network(_vni_map())
    assert ops[0]["ip"] != show[0]["ip"]
    assert ops[0]["ip"].endswith(".4")
    assert show[0]["ip"].endswith(".3")
    # Same subnet / gateway so both pods share the transit net.
    assert ops[0]["cidr"] == show[0]["cidr"]
    assert ops[0]["gateway"] == show[0]["gateway"]


def test_ops_pod_infra_network_carries_dns_nameserver():
    nets = ops_pod_infra_network(_vni_map(), dns_nameserver="10.0.0.53")
    assert nets[0]["dns_nameserver"] == "10.0.0.53"


def test_ops_pod_infra_network_empty_when_no_vni():
    assert ops_pod_infra_network({}) == []


def _clusters() -> list[dict]:
    return [
        {
            "id": "cl-1",
            "name": "prod",
            "_generatedInstallConfig": "install: prod\n",
            "_generatedAgentConfig": "agent: prod\n",
        },
        {
            "id": "cl-2",
            "name": "edge",
            "_generatedInstallConfig": "install: edge\n",
            "_generatedAgentConfig": "agent: edge\n",
        },
    ]


def _build():
    project = SimpleNamespace(id="proj-123", name="demo")
    return build_ops_pod_config(
        project=project,
        clusters=_clusters(),
        api_url="https://troshka.example.com",
        api_key="trk_secret",
        ocp_version="4.20.0",
        pull_secret_json='{"auths":{}}',
    )


def test_build_ops_pod_config_pod_shape():
    cfg = _build()
    assert cfg["pod_name"] == "ops"
    assert cfg["restart_policy"] == "always"
    assert cfg["privileged"] is True
    assert len(cfg["containers"]) == 1


def test_build_ops_pod_config_main_container_image_and_env():
    cfg = _build()
    main = cfg["containers"][0]
    assert main["image"] == OPS_POD_IMAGE
    env = main["env"]
    assert env["TROSHKA_API_URL"] == "https://troshka.example.com"
    assert env["TROSHKA_API_KEY"] == "trk_secret"
    assert env["TROSHKA_PROJECT_ID"] == "proj-123"
    assert env["OCP_VERSION"] == "4.20.0"


def test_build_ops_pod_config_carries_all_cluster_configs():
    cfg = _build()
    files = cfg["files"]
    # Both clusters' install AND agent configs are present, scoped by id.
    assert files["cl-1/install-config.yaml"] == "install: prod\n"
    assert files["cl-1/agent-config.yaml"] == "agent: prod\n"
    assert files["cl-2/install-config.yaml"] == "install: edge\n"
    assert files["cl-2/agent-config.yaml"] == "agent: edge\n"


def test_build_ops_pod_config_includes_pull_secret():
    cfg = _build()
    assert cfg["files"]["pull-secret.json"] == '{"auths":{}}'


def test_build_ops_pod_config_workdir_mounted():
    cfg = _build()
    workdir = cfg["workdir"]
    assert workdir
    mounts = cfg["containers"][0]["mounts"]
    assert any(m.get("mountPath") == workdir for m in mounts)
