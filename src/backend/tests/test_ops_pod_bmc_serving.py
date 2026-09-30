"""Troshkad ops-pod must serve the agent ISO on the BMC network.

Sushy-emulator runs in the project netns on the BMC bridge. Serving from the
infra-transit IP (hostname -I → 172.30.x.4) is unroutable for that fetch, so
InsertMedia reports success while the domain never gets a CDROM.
"""


def _topo_with_bmc(bmc_cidr="192.168.100.0/24"):
    return {
        "nodes": [
            {
                "type": "networkNode",
                "id": "cluster",
                "data": {"subtype": "network", "cidr": "10.0.0.0/24"},
            },
            {
                "type": "networkNode",
                "id": "bmc",
                "data": {
                    "subtype": "network",
                    "networkType": "bmc",
                    "cidr": bmc_cidr,
                },
            },
        ]
    }


def test_ops_pod_bmc_serving_network_returns_bridge_and_ip():
    from app.services.ocp.ops_pod_scaffold import ops_pod_bmc_serving_network

    net, serving = ops_pod_bmc_serving_network(
        _topo_with_bmc(), "2d7a8b32-d452-4eaa-a4fe-e09d5d72f10c"
    )
    assert serving == "192.168.100.50"
    assert net["bridge"] == "br-bmc-2d7a8b32"
    assert net["ip"] == "192.168.100.50"
    assert net["cidr"] == "192.168.100.0/24"
    assert net.get("infra_transit") is not True


def test_ops_pod_bmc_serving_network_absent_without_bmc():
    from app.services.ocp.ops_pod_scaffold import ops_pod_bmc_serving_network

    topo = {
        "nodes": [
            {
                "type": "networkNode",
                "id": "cluster",
                "data": {"subtype": "network", "cidr": "10.0.0.0/24"},
            }
        ]
    }
    net, serving = ops_pod_bmc_serving_network(
        topo, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    assert net is None
    assert serving is None


def test_troshkad_ops_pod_networks_appends_bmc_and_serving_ip():
    from app.services.ocp.ops_pod_scaffold import troshkad_ops_pod_networks

    nets, serving = troshkad_ops_pod_networks(
        topology=_topo_with_bmc(),
        project_id="2d7a8b32-d452-4eaa-a4fe-e09d5d72f10c",
        vni_map={1001: 1001},
        dns_nameserver="10.0.0.1",
    )
    assert serving == "192.168.100.50"
    assert nets[0].get("infra_transit") is True
    assert any(n.get("bridge") == "br-bmc-2d7a8b32" for n in nets)


def test_ops_pod_create_params_passes_serving_ip(monkeypatch):
    from app.services import deploy_service

    captured = {}

    def _fake_command(*_a, **kwargs):
        captured["serving_ip"] = kwargs.get("serving_ip")
        return ["bash", "-c", "true"]

    monkeypatch.setattr(deploy_service, "_ops_pod_command", _fake_command)
    monkeypatch.setattr(
        deploy_service, "_resolve_ops_pod_pull_through_registry", lambda *a, **k: None
    )
    monkeypatch.setattr(
        deploy_service,
        "_partition_ops_pod_clusters",
        lambda topo, clusters: ([], clusters),
    )

    from app.services.ocp import ops_pod_scaffold as scaffold

    monkeypatch.setattr(scaffold, "ops_pod_config_files", lambda *a, **k: {})
    monkeypatch.setattr(
        scaffold,
        "troshkad_ops_pod_networks",
        lambda **k: (
            [
                {
                    "bridge": "",
                    "ip": "172.30.5.4",
                    "cidr": "172.30.5.0/24",
                    "gateway": "172.30.5.2",
                    "infra_transit": True,
                },
                {
                    "bridge": "br-bmc-2d7a8b32",
                    "ip": "192.168.100.50",
                    "cidr": "192.168.100.0/24",
                },
            ],
            "192.168.100.50",
        ),
    )

    class _P:
        id = "2d7a8b32-d452-4eaa-a4fe-e09d5d72f10c"

    params = deploy_service._ops_pod_create_params(
        s=None,
        project=_P(),
        clusters=[{"id": "ocp", "name": "ocp"}],
        topology=_topo_with_bmc(),
        vni_map={1001: 1001},
        api_url="http://api.example",
        api_key="key",
        ocp_version="4.22",
        pull_secret_json="{}",
    )
    assert captured["serving_ip"] == "192.168.100.50"
    assert "br-bmc-2d7a8b32" in [n["bridge"] for n in params["networks"]]


def test_redfish_insert_media_fails_when_not_inserted():
    """InsertMedia must not be swallowed with || true — empty CDROM is fatal."""
    from app.services.ocp.agent_template import _redfish_insert_media_cmd

    cmd = _redfish_insert_media_cmd("  ", "192.168.100.11")
    assert "VirtualMedia.InsertMedia" in cmd
    insert_block = cmd.split("InsertMedia")[1].split("ComputerSystem.Reset")[0]
    assert "|| true" not in insert_block
    assert "Inserted" in cmd
    assert "exit 1" in cmd
