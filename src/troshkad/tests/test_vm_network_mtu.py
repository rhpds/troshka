# src/troshkad/tests/test_vm_network_mtu.py
import xml.etree.ElementTree as ET
import os
import stat
from unittest.mock import patch
import pytest
import troshkad


@pytest.mark.parametrize("fail_define", [False, True])
def test_nested_mac_xml_is_private_and_cleaned_up(fail_define):
    xml = '<domain><devices><interface><source bridge="br-1001"/></interface></devices></domain>'
    paths = []

    def define(job, command, timeout):
        assert command[:2] == ["virsh", "define"]
        path = command[2]
        paths.append(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert ET.parse(path).find(".//driver").get("trustGuestRxFilters") == "yes"
        if fail_define:
            raise RuntimeError("define failed")

    with patch("troshkad.subprocess.check_output", return_value=xml), patch(
        "troshkad._run_cmd", side_effect=define
    ), patch("troshkad._job_log"):
        if fail_define:
            with pytest.raises(RuntimeError, match="define failed"):
                troshkad._apply_nested_mac_trust(
                    {}, "test-domain", [{"bridge": "br-1001", "allow_nested_macs": True}]
                )
        else:
            troshkad._apply_nested_mac_trust(
                {}, "test-domain", [{"bridge": "br-1001", "allow_nested_macs": True}]
            )
    assert len(paths) == 1
    assert not os.path.exists(paths[0])


def test_network_arg_includes_mtu_size():
    net = {"bridge": "br-1001", "model": "virtio", "mtu": 8850}
    arg = troshkad._network_arg(net)
    assert arg == "bridge=br-1001,model=virtio,mtu.size=8850"


def test_network_arg_omits_mtu_when_absent():
    net = {"bridge": "br-1001", "model": "virtio"}
    assert "mtu.size" not in troshkad._network_arg(net)


def test_reconfigure_nics_sets_trust_guest_rx_filters():
    root = ET.fromstring("<domain><devices/></domain>")
    nics = [
        {
            "mac": "aa:bb:cc:dd:ee:ff",
            "bridge": "br-1001",
            "model": "virtio",
            "allow_nested_macs": True,
        }
    ]
    troshkad._reconfigure_nics(root, nics)
    iface = root.find(".//interface")
    driver = iface.find("driver")
    assert driver is not None
    assert driver.get("trustGuestRxFilters") == "yes"


def test_reconfigure_nics_omits_trust_by_default():
    root = ET.fromstring("<domain><devices/></domain>")
    nics = [{"mac": "aa:bb:cc:dd:ee:ff", "bridge": "br-1001", "model": "virtio"}]
    troshkad._reconfigure_nics(root, nics)
    iface = root.find(".//interface")
    assert iface.find("driver") is None


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
