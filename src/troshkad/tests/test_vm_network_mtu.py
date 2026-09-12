# src/troshkad/tests/test_vm_network_mtu.py
from unittest.mock import patch, MagicMock
import troshkad


def test_network_arg_includes_mtu_size():
    net = {"bridge": "br-1001", "model": "virtio", "mtu": 8850}
    arg = troshkad._network_arg(net)
    assert arg == "bridge=br-1001,model=virtio,mtu.size=8850"


def test_network_arg_omits_mtu_when_absent():
    net = {"bridge": "br-1001", "model": "virtio"}
    assert "mtu.size" not in troshkad._network_arg(net)


def test_setup_vxlan_bridge_sets_mtu():
    """Test that _setup_vxlan_bridge sets bridge MTU when mtu is present."""
    job = {"job_id": "test-job", "output": []}
    net = {
        "vni": 1001,
        "bridge_name": "br-1001",
        "vxlan_name": "vxlan-1001",
        "cidr": "10.1.0.0/24",
        "peers": [],
        "mtu": 8850,
    }
    ns = "troshka-testpid"
    host_ip = "10.0.0.1"
    pid = "testpid"

    with patch("troshkad._run_cmd") as mock_run:
        with patch("troshkad._add_vxlan_fdb_peers"):
            with patch("troshkad._attach_vxlan_to_ns_bridge"):
                with patch("troshkad._ensure_host_dummy_bridge"):
                    with patch("troshkad._assign_bridge_gateway_ip"):
                        troshkad._setup_vxlan_bridge(job, ns, host_ip, net, pid)

    # Find the bridge MTU call
    mtu_calls = [
        call
        for call in mock_run.call_args_list
        if len(call[0]) > 1
        and call[0][1] == ["ip", "link", "set", "br-1001", "mtu", "8850"]
    ]
    assert len(mtu_calls) == 1, f"Expected 1 MTU call, found {len(mtu_calls)}"
