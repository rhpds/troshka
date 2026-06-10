from app.services.haproxy_config import generate_haproxy_config
from app.services.vxlan import build_host_network_config


def test_generate_basic_tcp_config():
    frontends = [
        {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
        {"name": "ingress-https", "bindPort": 443, "mode": "tcp", "backendPort": 443},
    ]
    backends = [
        {"name": "cp-0", "ip": "10.0.0.10"},
        {"name": "cp-1", "ip": "10.0.0.11"},
        {"name": "cp-2", "ip": "10.0.0.12"},
    ]
    config = generate_haproxy_config(frontends, backends)

    assert "frontend api" in config
    assert "bind *:6443" in config
    assert "default_backend api-servers" in config
    assert "server cp-0 10.0.0.10:6443 check" in config
    assert "server cp-1 10.0.0.11:6443 check" in config
    assert "server cp-2 10.0.0.12:6443 check" in config
    assert "frontend ingress-https" in config
    assert "bind *:443" in config


def test_generate_config_with_health_check():
    frontends = [
        {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
    ]
    backends = [
        {"name": "cp-0", "ip": "10.0.0.10"},
    ]
    config = generate_haproxy_config(frontends, backends)
    assert "balance roundrobin" in config
    assert "mode tcp" in config


def test_generate_config_empty_backends():
    frontends = [
        {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
    ]
    config = generate_haproxy_config(frontends, backends=[])
    assert "frontend api" in config
    assert "backend api-servers" in config


def test_generate_config_global_and_defaults():
    config = generate_haproxy_config(
        [{"name": "x", "bindPort": 80, "mode": "tcp", "backendPort": 80}],
        [{"name": "s1", "ip": "10.0.0.1"}],
    )
    assert "global" in config
    assert "maxconn" in config
    assert "timeout connect" in config
    assert "timeout client" in config
    assert "timeout server" in config


def test_build_lb_config_from_topology():
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "name": "cluster",
                    "cidr": "10.0.0.0/24",
                    "dhcp": True,
                },
            },
            {
                "id": "lb-1",
                "type": "networkNode",
                "data": {
                    "subtype": "loadbalancer",
                    "networkType": "loadbalancer",
                    "name": "ocp-lb",
                    "frontends": [
                        {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
                        {"name": "ingress", "bindPort": 443, "mode": "tcp", "backendPort": 443},
                    ],
                },
            },
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "name": "cp-0",
                    "nics": [{"id": "nic-1", "ip": "10.0.0.10", "mac": "52:54:00:aa:bb:01"}],
                },
            },
            {
                "id": "vm-2",
                "type": "vmNode",
                "data": {
                    "name": "cp-1",
                    "nics": [{"id": "nic-2", "ip": "10.0.0.11", "mac": "52:54:00:aa:bb:02"}],
                },
            },
        ],
        "edges": [
            {"source": "net-1", "target": "vm-1", "sourceHandle": "net-1-bottom", "targetHandle": "nic-nic-1-top"},
            {"source": "net-1", "target": "vm-2", "sourceHandle": "net-1-bottom", "targetHandle": "nic-nic-2-top"},
            {"source": "lb-1", "target": "vm-1", "sourceHandle": "lb-1-bottom", "targetHandle": "nic-nic-1-top"},
            {"source": "lb-1", "target": "vm-2", "sourceHandle": "lb-1-bottom", "targetHandle": "nic-nic-2-top"},
        ],
    }
    vni_map = {"net-1": 100}

    result = build_host_network_config(topology, vni_map, peer_ips=[])
    assert result.get("loadbalancer") is not None
    lb = result["loadbalancer"]
    assert len(lb["frontends"]) == 2
    assert lb["frontends"][0]["name"] == "api"
    assert len(lb["backends"]) == 2
    backend_ips = {b["ip"] for b in lb["backends"]}
    assert "10.0.0.10" in backend_ips
    assert "10.0.0.11" in backend_ips
