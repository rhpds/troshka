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


def test_preview_inventory_maps_groups_to_names():
    from app.services.workloads.inventory import preview_inventory

    topo = {
        "nodes": [
            {
                "id": "n1",
                "type": "vmNode",
                "data": {"name": "bastion", "tags": {"AnsibleGroup": "bastions, all"}},
            },
            {
                "id": "n2",
                "type": "vmNode",
                "data": {"name": "web1", "tags": {"AnsibleGroup": "web"}},
            },
            {"id": "n3", "type": "networkNode", "data": {"name": "net"}},
        ]
    }
    groups = preview_inventory(topo)
    assert groups["bastions"] == ["bastion"]
    assert groups["all"] == ["bastion"]
    assert groups["web"] == ["web1"]
    assert "net" not in {v for vs in groups.values() for v in vs}


def test_preview_inventory_empty():
    from app.services.workloads.inventory import preview_inventory

    assert preview_inventory({"nodes": []}) == {}
