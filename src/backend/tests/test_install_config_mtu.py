from app.services.ocp.agent_template import (
    _build_install_config,
    _cluster_network_mtu,
)


def test_cluster_network_mtu_subtracts_geneve_overhead():
    assert _cluster_network_mtu(8850) == 8750


def test_cluster_network_mtu_none_when_unset():
    assert _cluster_network_mtu(None) is None


def test_cluster_network_mtu_none_when_nonpositive():
    assert _cluster_network_mtu(0) is None


def test_install_config_emits_cluster_network_mtu():
    """Verify clusterNetworkMTU is emitted when machine network has resolved MTU."""
    topology = {
        "nodes": [
            {
                "id": "net1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "networkType": "internal",
                    "cidr": "192.168.1.0/24",
                    "mtu": 8850,
                },
            },
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "nics": [{"ip": "192.168.1.10", "network": "net1"}],
                    "nodeGroup": "control-plane",
                },
            },
        ]
    }
    cluster = {
        "id": "c1",
        "name": "test",
        "baseDomain": "test.local",
        "controlPlane": 1,
        "workers": 0,
    }
    members = [topology["nodes"][1]]
    ic_yaml = _build_install_config(
        cluster, members, topology, "fake-pull-secret", "fake-ssh-key"
    )
    assert "clusterNetworkMTU: 8750" in ic_yaml


def test_install_config_omits_cluster_network_mtu_when_none():
    """Verify clusterNetworkMTU is omitted when MTU is not resolved."""
    topology = {
        "nodes": [
            {
                "id": "net1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "networkType": "internal",
                    "cidr": "192.168.1.0/24",
                },
            },
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "nics": [{"ip": "192.168.1.10", "network": "net1"}],
                    "nodeGroup": "control-plane",
                },
            },
        ]
    }
    cluster = {
        "id": "c1",
        "name": "test",
        "baseDomain": "test.local",
        "controlPlane": 1,
        "workers": 0,
    }
    members = [topology["nodes"][1]]
    ic_yaml = _build_install_config(
        cluster, members, topology, "fake-pull-secret", "fake-ssh-key"
    )
    assert "clusterNetworkMTU" not in ic_yaml
