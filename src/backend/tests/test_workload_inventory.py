import pytest
import yaml

from app.services.workloads import inventory


def _vm(name, groups, ip="10.0.0.5", vm_id="v1"):
    return {
        "id": vm_id,
        "type": "vmNode",
        "data": {"name": name, "tags": {"AnsibleGroup": groups}, "nics": [{"ip": ip}]},
    }


def test_build_inventory_yaml_shape():
    text = inventory.build_inventory_yaml("https://api.example", "trk_abc", "p1")
    doc = yaml.safe_load(text)
    assert doc["plugin"] == "troshka.cloud.troshka"
    assert doc["api_url"] == "https://api.example"
    assert doc["api_key"] == "trk_abc"
    assert doc["project_id"] == "p1"
    assert doc["connection_mode"] == "ssh"


def test_validate_ok_with_bastion():
    topo = {
        "nodes": [_vm("b", "bastions"), _vm("m", "masters", vm_id="v2")],
        "externalIps": [{"vmId": "v1", "ip": "1.2.3.4"}],
    }
    inventory.validate_ansible_groups(topo, require_bastion=True)  # no raise


def test_validate_missing_ansible_group_raises():
    topo = {
        "nodes": [
            {
                "id": "v1",
                "type": "vmNode",
                "data": {"name": "x", "tags": {}, "nics": [{"ip": "10.0.0.1"}]},
            }
        ]
    }
    with pytest.raises(inventory.InventoryError):
        inventory.validate_ansible_groups(topo, require_bastion=False)


def test_validate_requires_exactly_one_bastion():
    topo = {
        "nodes": [_vm("b1", "bastions"), _vm("b2", "bastions", vm_id="v2")],
        "externalIps": [
            {"vmId": "v1", "ip": "1.2.3.4"},
            {"vmId": "v2", "ip": "5.6.7.8"},
        ],
    }
    with pytest.raises(inventory.InventoryError):
        inventory.validate_ansible_groups(topo, require_bastion=True)


def test_validate_bastion_needs_external_ip():
    topo = {"nodes": [_vm("b", "bastions")], "externalIps": []}
    with pytest.raises(inventory.InventoryError):
        inventory.validate_ansible_groups(topo, require_bastion=True)


def test_validate_vm_names_success():
    """validate_vm_names accepts all VMs with first-NIC IPs."""
    from app.services.workloads.inventory import validate_vm_names

    topo = {
        "nodes": [
            _vm("vm1", "workers", ip="10.0.0.1", vm_id="v1"),
            _vm("vm2", "masters", ip="10.0.0.2", vm_id="v2"),
        ]
    }
    # No raise
    validate_vm_names(topo, ["vm1", "vm2"])


def test_validate_vm_names_missing_vm_raises():
    """validate_vm_names raises InventoryError when a named VM is missing."""
    from app.services.workloads.inventory import InventoryError, validate_vm_names

    topo = {"nodes": [_vm("vm1", "workers", ip="10.0.0.1")]}
    with pytest.raises(InventoryError, match="VM missing-vm not found"):
        validate_vm_names(topo, ["vm1", "missing-vm"])


def test_validate_vm_names_missing_ip_raises():
    """validate_vm_names raises InventoryError when a VM has no first-NIC IP."""
    from app.services.workloads.inventory import InventoryError, validate_vm_names

    topo = {
        "nodes": [
            {
                "id": "v1",
                "type": "vmNode",
                "data": {"name": "vm1", "tags": {}, "nics": []},
            }
        ]
    }
    with pytest.raises(InventoryError, match="VM vm1 has no first-NIC IP"):
        validate_vm_names(topo, ["vm1"])
