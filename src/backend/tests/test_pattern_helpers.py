"""Tests for extracted helper functions in pattern_service.py."""

from app.services.pattern_service import _build_disk_to_vm_map

# ---------------------------------------------------------------------------
# Topology builder helpers
# ---------------------------------------------------------------------------


def _vm_node(node_id, label="test-vm"):
    return {"id": node_id, "type": "vmNode", "data": {"label": label}}


def _storage_node(node_id, size_gb=50, fmt="qcow2"):
    return {
        "id": node_id,
        "type": "storageNode",
        "data": {"size": size_gb, "format": fmt},
    }


def _network_node(node_id, cidr="10.0.0.0/24"):
    return {"id": node_id, "type": "networkNode", "data": {"cidr": cidr}}


def _edge(source, target, source_handle="storage", target_handle="vm"):
    return {
        "source": source,
        "target": target,
        "sourceHandle": source_handle,
        "targetHandle": target_handle,
    }


# ---------------------------------------------------------------------------
# _build_disk_to_vm_map
# ---------------------------------------------------------------------------


class TestBuildDiskToVmMap:
    """Tests for _build_disk_to_vm_map()."""

    def test_two_vms_three_disks(self):
        """Two VMs with one and two disks respectively."""
        topology = {
            "nodes": [
                _vm_node("vm1", "web-server"),
                _vm_node("vm2", "db-server"),
                _storage_node("disk1", 50),
                _storage_node("disk2", 100),
                _storage_node("disk3", 200),
            ],
            "edges": [
                _edge("vm1", "disk1"),
                _edge("vm2", "disk2"),
                _edge("vm2", "disk3"),
            ],
        }

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        assert len(disk_nodes) == 3
        assert set(vm_nodes.keys()) == {"vm1", "vm2"}

        # disk_to_vm mapping
        assert disk_to_vm["disk1"] == "vm1"
        assert disk_to_vm["disk2"] == "vm2"
        assert disk_to_vm["disk3"] == "vm2"

        # vm_to_disks mapping
        assert len(vm_to_disks["vm1"]) == 1
        assert len(vm_to_disks["vm2"]) == 2
        assert vm_to_disks["vm1"][0]["id"] == "disk1"
        disk_ids_for_vm2 = {d["id"] for d in vm_to_disks["vm2"]}
        assert disk_ids_for_vm2 == {"disk2", "disk3"}

    def test_vm_with_multiple_disks(self):
        """Single VM with four disks."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _storage_node("d1", 10),
                _storage_node("d2", 20),
                _storage_node("d3", 30),
                _storage_node("d4", 40),
            ],
            "edges": [
                _edge("vm1", "d1"),
                _edge("vm1", "d2"),
                _edge("vm1", "d3"),
                _edge("vm1", "d4"),
            ],
        }

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        assert len(disk_nodes) == 4
        assert len(vm_nodes) == 1
        assert all(vm_id == "vm1" for vm_id in disk_to_vm.values())
        assert len(vm_to_disks["vm1"]) == 4

    def test_unconnected_storage_node(self):
        """Storage node not connected to any VM is in disk_nodes but not in mappings."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _storage_node("disk1", 50),
                _storage_node("orphan_disk", 100),
            ],
            "edges": [
                _edge("vm1", "disk1"),
            ],
        }

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        assert len(disk_nodes) == 2
        assert "orphan_disk" not in disk_to_vm
        assert len(vm_to_disks["vm1"]) == 1

    def test_empty_topology(self):
        """Empty topology returns empty structures."""
        topology = {"nodes": [], "edges": []}

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        assert disk_nodes == []
        assert vm_nodes == {}
        assert disk_to_vm == {}
        assert vm_to_disks == {}

    def test_missing_keys_topology(self):
        """Topology dict with no nodes/edges keys (fully empty)."""
        topology = {}

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        assert disk_nodes == []
        assert vm_nodes == {}
        assert disk_to_vm == {}
        assert vm_to_disks == {}

    def test_no_storage_nodes(self):
        """Topology with VMs and networks but no storage nodes."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _vm_node("vm2"),
                _network_node("net1"),
            ],
            "edges": [
                _edge("vm1", "net1", "network", "vm"),
            ],
        }

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        assert disk_nodes == []
        assert set(vm_nodes.keys()) == {"vm1", "vm2"}
        assert disk_to_vm == {}
        assert vm_to_disks == {}

    def test_reverse_edge_direction(self):
        """Edge with storage as source and VM as target (reverse direction)."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _storage_node("disk1", 50),
            ],
            "edges": [
                # Reversed: storage is source, VM is target
                _edge("disk1", "vm1", "vm", "storage"),
            ],
        }

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        # The function handles both edge directions
        assert disk_to_vm["disk1"] == "vm1"
        assert len(vm_to_disks["vm1"]) == 1

    def test_network_edges_ignored(self):
        """Edges between VMs and networks are not treated as disk mappings."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _storage_node("disk1", 50),
                _network_node("net1"),
            ],
            "edges": [
                _edge("vm1", "disk1"),
                _edge("vm1", "net1", "network", "vm"),
            ],
        }

        disk_nodes, vm_nodes, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        # Only disk1 mapped, net1 ignored
        assert len(disk_to_vm) == 1
        assert "disk1" in disk_to_vm
        assert len(vm_to_disks) == 1

    def test_vm_nodes_returns_dict_keyed_by_id(self):
        """vm_nodes_by_id is a dict with node id keys and full node dicts as values."""
        topology = {
            "nodes": [
                _vm_node("vm-abc", "my-server"),
                _storage_node("disk1"),
            ],
            "edges": [_edge("vm-abc", "disk1")],
        }

        _, vm_nodes, _, _ = _build_disk_to_vm_map(topology)

        assert "vm-abc" in vm_nodes
        assert vm_nodes["vm-abc"]["data"]["label"] == "my-server"
        assert vm_nodes["vm-abc"]["type"] == "vmNode"

    def test_disk_nodes_preserve_data(self):
        """Disk nodes returned preserve their original data attributes."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _storage_node("disk1", 120, "raw"),
            ],
            "edges": [_edge("vm1", "disk1")],
        }

        disk_nodes, _, _, vm_to_disks = _build_disk_to_vm_map(topology)

        assert disk_nodes[0]["data"]["size"] == 120
        assert disk_nodes[0]["data"]["format"] == "raw"
        assert vm_to_disks["vm1"][0]["data"]["size"] == 120

    def test_iso_disks_still_mapped(self):
        """ISO-format storage nodes are still returned (filtering is caller's job)."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _storage_node("disk1", 50, "qcow2"),
                _storage_node("iso1", 4, "iso"),
            ],
            "edges": [
                _edge("vm1", "disk1"),
                _edge("vm1", "iso1"),
            ],
        }

        disk_nodes, _, disk_to_vm, vm_to_disks = _build_disk_to_vm_map(topology)

        # Both mapped; caller filters by format
        assert len(disk_nodes) == 2
        assert "disk1" in disk_to_vm
        assert "iso1" in disk_to_vm
        assert len(vm_to_disks["vm1"]) == 2

    def test_edge_between_two_vms_ignored(self):
        """An edge between two VMs does not produce a disk mapping."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _vm_node("vm2"),
                _storage_node("disk1"),
            ],
            "edges": [
                _edge("vm1", "vm2"),  # VM-to-VM edge
                _edge("vm1", "disk1"),
            ],
        }

        _, _, disk_to_vm, _ = _build_disk_to_vm_map(topology)

        assert len(disk_to_vm) == 1
        assert disk_to_vm["disk1"] == "vm1"

    def test_edge_between_two_disks_ignored(self):
        """An edge between two storage nodes does not produce a mapping."""
        topology = {
            "nodes": [
                _vm_node("vm1"),
                _storage_node("disk1"),
                _storage_node("disk2"),
            ],
            "edges": [
                _edge("disk1", "disk2"),  # disk-to-disk edge
                _edge("vm1", "disk1"),
            ],
        }

        _, _, disk_to_vm, _ = _build_disk_to_vm_map(topology)

        assert len(disk_to_vm) == 1
        assert disk_to_vm["disk1"] == "vm1"
