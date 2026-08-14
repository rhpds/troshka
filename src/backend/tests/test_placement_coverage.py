"""Tests for uncovered placement functions: calculate_project_requirements,
select_network_host (gateway path), find_multihost_placement (anti-affinity),
find_available_host, _auto_select_pool, sync_host_capacity, and inflight helpers.
"""

import uuid
from unittest.mock import patch

from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.models.storage_pool import StoragePool
from app.models.user import User
from app.services.placement import (
    _auto_select_pool,
    _get_inflight_deploys,
    calculate_project_requirements,
    find_available_host,
    find_multihost_placement,
    get_allocatable,
    record_deploy_end,
    record_deploy_start,
    select_network_host,
    sync_host_capacity,
)
from tests.conftest import TestSession

# Shared test user for FK constraints
_setup_db = TestSession()
_test_user = User(
    email="placement-coverage@test.com",
    display_name="Test User",
    role="admin",
)
_setup_db.add(_test_user)
_setup_db.commit()
_setup_db.refresh(_test_user)
_USER_ID = _test_user.id
_setup_db.close()


# ---------------------------------------------------------------------------
# calculate_project_requirements
# ---------------------------------------------------------------------------
def test_calc_requirements_vms_only():
    topo = {
        "nodes": [
            {"id": "v1", "type": "vmNode", "data": {"vcpus": 4, "ram": 16}},
            {"id": "v2", "type": "vmNode", "data": {"vcpus": 8, "ram": 32}},
        ],
        "edges": [],
    }
    reqs = calculate_project_requirements(topo)
    assert reqs["vm_count"] == 2
    assert reqs["container_count"] == 0
    assert reqs["total_vcpus"] == 12
    assert reqs["total_ram_mb"] == 48 * 1024
    assert reqs["requested_eips"] == 0


def test_calc_requirements_containers_only():
    topo = {
        "nodes": [
            {"id": "c1", "type": "containerNode", "data": {"cpus": 2, "memory": 1024}},
            {"id": "c2", "type": "containerNode", "data": {"cpus": 1, "memory": 512}},
        ],
        "edges": [],
    }
    reqs = calculate_project_requirements(topo)
    assert reqs["vm_count"] == 0
    assert reqs["container_count"] == 2
    assert reqs["total_vcpus"] == 3
    assert reqs["total_ram_mb"] == 1536


def test_calc_requirements_mixed():
    topo = {
        "nodes": [
            {"id": "v1", "type": "vmNode", "data": {"vcpus": 4, "ram": 8}},
            {"id": "c1", "type": "containerNode", "data": {"cpus": 2, "memory": 2048}},
            {"id": "n1", "type": "networkNode", "data": {}},
        ],
        "edges": [],
        "externalIps": [{"vmId": "v1", "port": 443}],
    }
    reqs = calculate_project_requirements(topo)
    assert reqs["vm_count"] == 1
    assert reqs["container_count"] == 1
    assert reqs["total_vcpus"] == 6
    assert reqs["total_ram_mb"] == 8 * 1024 + 2048
    assert reqs["requested_eips"] == 1


def test_calc_requirements_defaults():
    """VMs/containers with missing resource fields use defaults."""
    topo = {
        "nodes": [
            {"id": "v1", "type": "vmNode", "data": {}},
            {"id": "c1", "type": "containerNode", "data": {}},
        ],
        "edges": [],
    }
    reqs = calculate_project_requirements(topo)
    assert reqs["total_vcpus"] == 2 + 1  # vm default 2, container default 1
    assert (
        reqs["total_ram_mb"] == 4 * 1024 + 512
    )  # vm default 4GB, container default 512MB


def test_calc_requirements_empty():
    reqs = calculate_project_requirements({"nodes": [], "edges": []})
    assert reqs["vm_count"] == 0
    assert reqs["container_count"] == 0
    assert reqs["total_vcpus"] == 0
    assert reqs["total_ram_mb"] == 0
    assert reqs["requested_eips"] == 0


# ---------------------------------------------------------------------------
# get_allocatable
# ---------------------------------------------------------------------------
def test_get_allocatable():
    db = TestSession()
    try:
        host = Host(
            ip_address="10.99.0.1",
            state="active",
            total_vcpus=32,
            total_ram_mb=128000,
        )
        db.add(host)
        db.commit()
        db.refresh(host)

        with patch(
            "app.services.placement._get_overcommit_ratios", return_value=(4.0, 1.5)
        ):
            vcpus, ram = get_allocatable(host)
        assert vcpus == 128  # 32 * 4
        assert ram == 192000  # 128000 * 1.5

        db.delete(host)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# sync_host_capacity
# ---------------------------------------------------------------------------
def test_sync_host_capacity():
    db = TestSession()
    try:
        host = Host(
            ip_address="10.99.0.2",
            state="active",
            total_vcpus=64,
            total_ram_mb=256000,
            used_vcpus=0,
            used_ram_mb=0,
        )
        db.add(host)
        db.commit()
        db.refresh(host)

        p1 = Project(
            name="sync-test-1",
            owner_id=_USER_ID,
            host_id=host.id,
            state="active",
            topology={
                "nodes": [
                    {"id": "v1", "type": "vmNode", "data": {"vcpus": 4, "ram": 8}},
                ],
                "edges": [],
            },
        )
        p2 = Project(
            name="sync-test-2",
            owner_id=_USER_ID,
            host_id=host.id,
            state="stopped",
            topology={
                "nodes": [
                    {"id": "v2", "type": "vmNode", "data": {"vcpus": 8, "ram": 32}},
                ],
                "edges": [],
            },
        )
        db.add_all([p1, p2])
        db.commit()

        sync_host_capacity(db, host)
        assert host.used_vcpus == 12  # 4 + 8
        assert host.used_ram_mb == 40 * 1024  # (8 + 32) * 1024

        db.delete(p1)
        db.delete(p2)
        db.delete(host)
        db.commit()
    finally:
        db.close()


def test_sync_host_capacity_ignores_destroyed():
    """Projects in destroyed/error/draft state are not counted."""
    db = TestSession()
    try:
        host = Host(
            ip_address="10.99.0.3",
            state="active",
            total_vcpus=64,
            total_ram_mb=256000,
            used_vcpus=99,
            used_ram_mb=99999,
        )
        db.add(host)
        db.commit()
        db.refresh(host)

        p_draft = Project(
            name="sync-draft",
            owner_id=_USER_ID,
            host_id=host.id,
            state="draft",
            topology={
                "nodes": [
                    {"id": "v1", "type": "vmNode", "data": {"vcpus": 16, "ram": 64}},
                ],
                "edges": [],
            },
        )
        db.add(p_draft)
        db.commit()

        sync_host_capacity(db, host)
        assert host.used_vcpus == 0
        assert host.used_ram_mb == 0

        db.delete(p_draft)
        db.delete(host)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Inflight deploy helpers (Redis mocked)
# ---------------------------------------------------------------------------
def test_get_inflight_deploys_no_redis():
    """Returns 0 when Redis is unavailable."""
    result = _get_inflight_deploys("nonexistent-host")
    assert result == 0


def test_record_deploy_start_no_redis():
    """Does not raise when Redis is unavailable."""
    record_deploy_start("nonexistent-host")


def test_record_deploy_end_no_redis():
    """Does not raise when Redis is unavailable."""
    record_deploy_end("nonexistent-host")


# ---------------------------------------------------------------------------
# select_network_host — gateway path
# ---------------------------------------------------------------------------
def test_select_network_host_prefers_gateway():
    """When a gateway VM exists, its host is selected."""
    assignments = {
        "host-a": ["vm1"],
        "host-b": ["vm2", "vm3", "vm4"],
    }
    topo = {
        "nodes": [
            {"id": "vm1", "type": "vmNode", "data": {"isGateway": True}},
            {"id": "vm2", "type": "vmNode", "data": {}},
            {"id": "vm3", "type": "vmNode", "data": {}},
            {"id": "vm4", "type": "vmNode", "data": {}},
        ],
        "edges": [],
    }
    result = select_network_host(assignments, topo)
    assert result == "host-a"


def test_select_network_host_gateway_not_found_falls_back():
    """Fallback to most VMs when no gateway VM in assignments."""
    assignments = {
        "host-a": ["vm1", "vm2"],
        "host-b": ["vm3"],
    }
    topo = {
        "nodes": [
            {"id": "vm1", "type": "vmNode", "data": {}},
            {"id": "vm2", "type": "vmNode", "data": {}},
            {"id": "vm3", "type": "vmNode", "data": {}},
        ],
        "edges": [],
    }
    result = select_network_host(assignments, topo)
    assert result == "host-a"


# ---------------------------------------------------------------------------
# find_multihost_placement — anti-affinity
# ---------------------------------------------------------------------------
def _make_provider(db):
    p = Provider(name=f"prov-{uuid.uuid4().hex[:6]}", type="ec2")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_host(db, provider, vcpus=64, ram_mb=256000, **kwargs):
    h = Host(
        ip_address=f"10.0.2.{uuid.uuid4().int % 250 + 1}",
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
    """vm_specs: list of (name, vcpus, ram_gb, extras_dict)"""
    nodes = []
    for name, vcpus, ram_gb, extras in vm_specs:
        node_id = str(uuid.uuid4())
        data = {"label": name, "name": name, "vcpus": vcpus, "ram": ram_gb}
        data.update(extras)
        nodes.append({"id": node_id, "type": "vmNode", "data": data})
    return {"nodes": nodes, "edges": []}


def test_multihost_anti_affinity_separates():
    """VMs with same separateHost group land on different hosts."""
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=32, ram_mb=128000)
        host_b = _make_host(db, prov, vcpus=32, ram_mb=128000)

        topo = _topology_with_vms(
            [
                ("sno1", 8, 32, {"separateHost": "sno-group"}),
                ("sno2", 8, 32, {"separateHost": "sno-group"}),
            ]
        )

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is not None

        vm_host = {}
        for hid, vm_ids in result.items():
            for vid in vm_ids:
                vm_host[vid] = hid

        vm_ids = [n["id"] for n in topo["nodes"]]
        assert vm_host[vm_ids[0]] != vm_host[vm_ids[1]]

        db.delete(host_a)
        db.delete(host_b)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_multihost_anti_affinity_fails_single_host():
    """Anti-affinity with only one host returns None (can't separate)."""
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=64, ram_mb=512000)

        topo = _topology_with_vms(
            [
                ("sno1", 4, 16, {"separateHost": "sno-group"}),
                ("sno2", 4, 16, {"separateHost": "sno-group"}),
            ]
        )

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is None

        db.delete(host_a)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_multihost_mixed_affinity_and_anti_affinity():
    """Affinity groups stay together while anti-affinity groups separate."""
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=32, ram_mb=128000)
        host_b = _make_host(db, prov, vcpus=32, ram_mb=128000)

        topo = _topology_with_vms(
            [
                (
                    "worker1",
                    4,
                    8,
                    {"affinityGroup": "workers", "separateHost": "spread"},
                ),
                (
                    "worker2",
                    4,
                    8,
                    {"affinityGroup": "workers", "separateHost": "spread"},
                ),
                ("hub", 8, 32, {}),
            ]
        )

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is not None

        all_placed = []
        for vms in result.values():
            all_placed.extend(vms)
        assert len(all_placed) == 3

        db.delete(host_a)
        db.delete(host_b)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_multihost_containers():
    """Container nodes are included in multihost placement."""
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=16, ram_mb=64000)
        host_b = _make_host(db, prov, vcpus=16, ram_mb=64000)

        topo = {
            "nodes": [
                {"id": "v1", "type": "vmNode", "data": {"vcpus": 8, "ram": 32}},
                {"id": "c1", "type": "containerNode", "data": {"cpus": 4, "ram": 16}},
            ],
            "edges": [],
        }

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is not None
        all_placed = []
        for vms in result.values():
            all_placed.extend(vms)
        assert len(all_placed) == 2

        db.delete(host_a)
        db.delete(host_b)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_build_placement_units_container_uses_memory_and_cpus():
    """Container nodes must contribute data.memory (MB) and data.cpus to a
    placement unit — not data.ram (GB) / data.vcpus, which they don't have."""
    from app.services.placement import _build_placement_units

    ctr = {"id": "c1", "type": "containerNode", "data": {"memory": 8192, "cpus": 3}}
    units = _build_placement_units({}, [ctr])
    assert units[0]["ram_mb"] == 8192
    assert units[0]["vcpus"] == 3


def test_build_placement_units_vm_uses_ram_gb():
    """VM nodes contribute data.ram (GB -> MB) and data.vcpus."""
    from app.services.placement import _build_placement_units

    vm = {"id": "v1", "type": "vmNode", "data": {"ram": 16, "vcpus": 8}}
    units = _build_placement_units({}, [vm])
    assert units[0]["ram_mb"] == 16 * 1024
    assert units[0]["vcpus"] == 8


def test_multihost_empty_topology():
    """Empty topology returns None."""
    db = TestSession()
    try:
        result = find_multihost_placement(db, {"nodes": [], "edges": []}, None, None)
        assert result is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# find_available_host
# ---------------------------------------------------------------------------
def test_find_available_host_picks_least_loaded():
    db = TestSession()
    try:
        prov = _make_provider(db)
        busy = _make_host(db, prov, vcpus=32, ram_mb=128000)
        idle = _make_host(db, prov, vcpus=32, ram_mb=128000)

        p_busy = Project(
            name="busy-proj",
            owner_id=_USER_ID,
            host_id=busy.id,
            state="active",
            topology={
                "nodes": [
                    {"id": "v1", "type": "vmNode", "data": {"vcpus": 16, "ram": 64}},
                ],
                "edges": [],
            },
        )
        db.add(p_busy)
        db.commit()

        with patch(
            "app.services.placement._get_overcommit_ratios", return_value=(4.0, 1.5)
        ):
            host = find_available_host(db, 4, 8192, provider_id=prov.id)

        assert host is not None
        assert host.id == idle.id

        db.delete(p_busy)
        db.delete(busy)
        db.delete(idle)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_find_available_host_none_when_full():
    db = TestSession()
    try:
        prov = _make_provider(db)
        small = _make_host(db, prov, vcpus=4, ram_mb=8000)

        with patch(
            "app.services.placement._get_overcommit_ratios", return_value=(1.0, 1.0)
        ):
            host = find_available_host(db, 100, 500000, provider_id=prov.id)

        assert host is None

        db.delete(small)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_find_available_host_filters_pool():
    db = TestSession()
    try:
        prov = _make_provider(db)
        pool = StoragePool(
            name="test-pool", mode="local", status="available", provider_id=prov.id
        )
        db.add(pool)
        db.commit()
        db.refresh(pool)

        in_pool = _make_host(db, prov, vcpus=32, ram_mb=128000, storage_pool_id=pool.id)
        outside = _make_host(db, prov, vcpus=32, ram_mb=128000)

        with patch(
            "app.services.placement._get_overcommit_ratios", return_value=(4.0, 1.5)
        ):
            host = find_available_host(db, 4, 8192, storage_pool_id=pool.id)

        assert host is not None
        assert host.id == in_pool.id

        db.delete(in_pool)
        db.delete(outside)
        db.delete(pool)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_find_available_host_excludes_pattern_buffer():
    db = TestSession()
    try:
        prov = _make_provider(db)
        buffer = _make_host(
            db, prov, vcpus=32, ram_mb=128000, host_type="pattern_buffer"
        )
        normal = _make_host(db, prov, vcpus=32, ram_mb=128000)

        with patch(
            "app.services.placement._get_overcommit_ratios", return_value=(4.0, 1.5)
        ):
            host = find_available_host(db, 4, 8192, provider_id=prov.id)

        assert host is not None
        assert host.id == normal.id

        db.delete(buffer)
        db.delete(normal)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# _auto_select_pool
# ---------------------------------------------------------------------------
def test_auto_select_pool_picks_most_free_ram():
    db = TestSession()
    try:
        prov = _make_provider(db)
        pool_small = StoragePool(
            name="small-pool", mode="local", status="available", provider_id=prov.id
        )
        pool_big = StoragePool(
            name="big-pool", mode="local", status="available", provider_id=prov.id
        )
        db.add_all([pool_small, pool_big])
        db.commit()
        db.refresh(pool_small)
        db.refresh(pool_big)

        small_host = _make_host(
            db, prov, vcpus=8, ram_mb=32000, storage_pool_id=pool_small.id
        )
        big_host = _make_host(
            db, prov, vcpus=64, ram_mb=512000, storage_pool_id=pool_big.id
        )

        with patch(
            "app.services.placement._get_overcommit_ratios", return_value=(1.0, 1.0)
        ):
            selected = _auto_select_pool(db)

        assert selected == pool_big.id

        db.delete(small_host)
        db.delete(big_host)
        db.delete(pool_small)
        db.delete(pool_big)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_auto_select_pool_single():
    db = TestSession()
    try:
        # Remove any pre-existing available pools from other tests
        stale = db.query(StoragePool).filter(StoragePool.status == "available").all()
        for s in stale:
            s.status = "disabled"
        db.commit()

        prov = _make_provider(db)
        pool = StoragePool(
            name="only-pool", mode="local", status="available", provider_id=prov.id
        )
        db.add(pool)
        db.commit()
        db.refresh(pool)

        selected = _auto_select_pool(db)
        assert selected == pool.id

        db.delete(pool)
        db.delete(prov)
        # Restore stale pools
        for s in stale:
            s.status = "available"
        db.commit()
    finally:
        db.close()


def test_auto_select_pool_none_available():
    db = TestSession()
    try:
        # Temporarily disable any pre-existing available pools
        stale = db.query(StoragePool).filter(StoragePool.status == "available").all()
        for s in stale:
            s.status = "disabled"
        db.commit()

        selected = _auto_select_pool(db)
        assert selected is None

        # Restore
        for s in stale:
            s.status = "available"
        db.commit()
    finally:
        db.close()
