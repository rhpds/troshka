"""Tests for BMC configuration extraction from topology."""

from app.services.deploy_service import _extract_bmc_config


def _make_topology(vms_with_bmc=None, include_bmc_network=True):
    """Build a minimal topology with optional BMC-enabled VMs."""
    nodes = []
    if include_bmc_network:
        nodes.append(
            {
                "id": "bmc-net-1",
                "type": "networkNode",
                "data": {
                    "name": "BMC Network",
                    "subtype": "network",
                    "networkType": "bmc",
                    "cidr": "192.168.100.0/24",
                    "dhcp": True,
                    "dns": False,
                    "bmcUsername": "admin",
                    "bmcPassword": "testpass123",
                },
            }
        )

    for i, (node_id, bmc_ip) in enumerate(vms_with_bmc or []):
        nodes.append(
            {
                "id": node_id,
                "type": "vmNode",
                "data": {
                    "name": f"vm-{i}",
                    "bmcEnabled": True,
                    "bmcIp": bmc_ip,
                    "vcpus": 2,
                    "ram": 4,
                },
            }
        )

    return {"nodes": nodes, "edges": []}


def test_extract_bmc_config_with_vms():
    topo = _make_topology(
        vms_with_bmc=[
            ("aaaaaaaa-1111-1111-1111-111111111111", "192.168.100.11"),
            ("bbbbbbbb-2222-2222-2222-222222222222", "192.168.100.12"),
        ]
    )
    result = _extract_bmc_config(topo, "cccccccc-3333-3333-3333-333333333333")
    assert result is not None
    assert len(result["vms"]) == 2
    assert result["vms"][0]["bmc_ip"] == "192.168.100.11"
    assert result["vms"][0]["domain_name"] == "troshka-cccccccc-aaaaaaaa"
    assert result["bmc_network"]["bmcPassword"] == "testpass123"


def test_extract_bmc_config_no_bmc_network():
    topo = _make_topology(include_bmc_network=False)
    result = _extract_bmc_config(topo, "cccccccc-3333-3333-3333-333333333333")
    assert result is None


def test_extract_bmc_config_no_bmc_vms():
    topo = _make_topology(vms_with_bmc=[])
    result = _extract_bmc_config(topo, "cccccccc-3333-3333-3333-333333333333")
    assert result is None
