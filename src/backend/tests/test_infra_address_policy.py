import pytest

from app.services.deploy_service import _kubevirt_ops_pod_net_ips
from app.services.kubevirt_reconfigure import _showroom_ip_for_cidr
from app.services.ocp.ops_pod_scaffold import _ops_lab_host_ip
from app.services.vxlan import _infra_ip_reservations


def test_ops_uses_six_on_lab_network():
    topology = {
        "nodes": [
            {
                "type": "networkNode",
                "data": {"subtype": "network", "cidr": "10.0.0.0/24"},
            }
        ]
    }
    assert _kubevirt_ops_pod_net_ips(topology) == ([("net1", "10.0.0.6/24")], None)
    assert _ops_lab_host_ip("192.168.100.0/24") == ("192.168.100.6", 24)


def test_ops_rejects_subnet_without_reserved_address():
    with pytest.raises(ValueError):
        _ops_lab_host_ip("10.0.0.0/30")


def test_showroom_does_not_fall_back_when_nine_is_taken():
    assert _showroom_ip_for_cidr("10.0.0.0/24", set()) == "10.0.0.9"
    with pytest.raises(ValueError, match="reserved"):
        _showroom_ip_for_cidr("10.0.0.0/24", {"10.0.0.9"})


def test_ops_address_is_reserved_from_dhcp():
    reservations = _infra_ip_reservations({"cidr": "10.0.0.0/24"}, set())
    assert any(r["ip"] == "10.0.0.6" for r in reservations)


def test_ops_rejects_existing_vm_at_six():
    topology = {"nodes": [{"type": "vmNode", "data": {"nics": [{"ip": "10.0.0.6"}]}}]}
    with pytest.raises(ValueError, match="reserved address"):
        _ops_lab_host_ip("10.0.0.0/24", topology)
