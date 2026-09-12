from unittest.mock import MagicMock, patch

from app.services.deploy_topology import (
    _find_vm_networks,
    network_mtu_map,
    resolve_topology_mtus,
)


def _topo():
    return {
        "nodes": [
            {"id": "net1", "type": "networkNode", "data": {"mtu": "auto"}},
            {"id": "net2", "type": "networkNode", "data": {"mtu": 9000}},
        ]
    }


def test_resolves_and_writes_back_concrete_mtu():
    topo = _topo()
    warnings = resolve_topology_mtus(topo, host_uplink_mtu=8900, spans_hosts=False)
    by_id = {n["id"]: n for n in topo["nodes"]}
    assert by_id["net1"]["data"]["mtu"] == 8900  # auto -> uplink
    assert by_id["net2"]["data"]["mtu"] == 8900  # 9000 clamped
    assert any("reduced" in w.lower() for w in warnings)


def test_per_network_mtu_map():
    m = network_mtu_map(_topo(), host_uplink_mtu=8900, spans_hosts=False)
    assert m == {"net1": 8900, "net2": 8900}


def test_net_dict_builder_includes_mtu():
    """Verify _find_vm_networks includes mtu in each net dict when mtu_map is given."""
    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "nics": [
                        {
                            "id": "nic-abc123",
                            "mac": "52:54:00:11:22:33",
                            "model": "virtio",
                        }
                    ]
                },
            },
            {"id": "net1", "type": "networkNode", "data": {}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "vm1",
                "target": "net1",
                "sourceHandle": "nic-abc123",
            }
        ],
    }
    vni_map = {"net1": 1001}
    mtu_map = {"net1": 8900}
    nets = _find_vm_networks("vm1", topo, vni_map, project_id="test", mtu_map=mtu_map)
    assert len(nets) == 1
    assert nets[0]["bridge"] == "br-1001"
    assert nets[0]["mac"] == "52:54:00:11:22:33"
    assert nets[0]["model"] == "virtio"
    assert nets[0]["mtu"] == 8900


def test_multihost_path_includes_mtu():
    """Verify _define_multihost_vms passes mtu_map to _create_vm_via_troshkad."""
    from app.services.deploy_service import _define_multihost_vms

    host = MagicMock(ip_address="10.0.0.1")
    topo = {
        "nodes": [
            {
                "id": "vm1",
                "type": "vmNode",
                "data": {
                    "nics": [
                        {"id": "nic-abc", "mac": "52:54:00:11:22:33", "model": "virtio"}
                    ]
                },
            },
            {"id": "net1", "type": "networkNode", "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "vm1", "target": "net1", "sourceHandle": "nic-abc"}
        ],
    }
    host_vms = [{"node_id": "vm1", "vcpus": 2, "ram_gb": 4, "uuid": "vm1"}]
    vni_map = {"net1": 1001}
    mtu_map = {"net1": 7500}
    pool = None
    clock_offset = None

    with patch("app.services.deploy_service._create_vm_via_troshkad") as mock_create:
        mock_create.return_value = "job-123"
        with patch("app.services.deploy_service.wait_for_job") as mock_wait:
            mock_wait.return_value = {
                "status": "completed",
                "result": {"domain_uuid": "dom-uuid"},
            }
            _define_multihost_vms(
                host,
                "proj-id",
                host_vms,
                topo,
                vni_map,
                pool,
                clock_offset,
                "host-label",
                mtu_map,
            )

    # Verify _create_vm_via_troshkad was called with mtu_map
    assert mock_create.call_count == 1
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["mtu_map"] == mtu_map
