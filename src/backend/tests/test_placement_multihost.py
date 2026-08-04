import uuid

from app.models.host import Host
from app.models.provider import Provider
from app.services.placement import (
    find_multihost_placement,
    select_network_host,
)
from tests.conftest import TestSession


def _make_provider(db):
    p = Provider(name=f"prov-{uuid.uuid4().hex[:6]}", type="ec2")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_host(db, provider, vcpus=64, ram_mb=256000, **kwargs):
    h = Host(
        ip_address=f"10.0.1.{uuid.uuid4().int % 250 + 1}",
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        total_vcpus=vcpus,
        total_ram_mb=ram_mb,
        used_vcpus=0,
        used_ram_mb=0,
        **kwargs,
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def _topology_with_vms(vm_specs):
    """vm_specs: list of (name, vcpus, ram_gb, affinity_group|None)"""
    nodes = []
    for name, vcpus, ram_gb, affinity in vm_specs:
        node_id = str(uuid.uuid4())
        nodes.append(
            {
                "id": node_id,
                "type": "vmNode",
                "data": {
                    "label": name,
                    "name": name,
                    "vcpus": vcpus,
                    "ram": ram_gb,
                    **({"affinityGroup": affinity} if affinity else {}),
                },
            }
        )
    return {"nodes": nodes, "edges": []}


def test_multihost_placement_basic():
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=16, ram_mb=64000)
        host_b = _make_host(db, prov, vcpus=16, ram_mb=64000)

        topo = _topology_with_vms(
            [
                ("vm1", 8, 16, None),
                ("vm2", 8, 16, None),
                ("vm3", 8, 16, None),
            ]
        )

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is not None
        all_vms = []
        for vms in result.values():
            all_vms.extend(vms)
        assert len(all_vms) == 3

        db.delete(host_a)
        db.delete(host_b)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_multihost_placement_respects_affinity():
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=16, ram_mb=64000)
        host_b = _make_host(db, prov, vcpus=16, ram_mb=64000)

        topo = _topology_with_vms(
            [
                ("worker1", 4, 16, "workers"),
                ("worker2", 4, 16, "workers"),
                ("hub", 8, 48, "hub"),
            ]
        )

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is not None

        vm_names_by_node_id = {n["id"]: n["data"]["name"] for n in topo["nodes"]}
        host_for_vm = {}
        for hid, vm_ids in result.items():
            for vid in vm_ids:
                host_for_vm[vm_names_by_node_id[vid]] = hid

        assert host_for_vm["worker1"] == host_for_vm["worker2"]

        db.delete(host_a)
        db.delete(host_b)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_multihost_placement_returns_none_when_impossible():
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=4, ram_mb=16000)

        topo = _topology_with_vms(
            [
                ("huge-vm", 64, 512, None),
            ]
        )

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is None

        db.delete(host_a)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_select_network_host_picks_most_vms():
    assignments = {
        "host-a": ["vm1"],
        "host-b": ["vm2", "vm3", "vm4"],
    }
    topo = {"nodes": [], "edges": []}
    result = select_network_host(assignments, topo)
    assert result == "host-b"
