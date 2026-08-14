"""
Placement service — assigns a project's VMs to available hosts.

Called when a user clicks Deploy. Finds a host with enough capacity
for the project's VMs, or fails if no host has room.
"""

import datetime
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.host import Host
from app.models.project import Project
from app.services.provisioner import provision_host
from app.services.vxlan import allocate_vnis_for_project, build_host_network_config

logger = logging.getLogger(__name__)


def calculate_project_requirements(topology: dict) -> dict:
    """Calculate total resource requirements from a project's topology."""
    nodes = topology.get("nodes", [])
    vms = [n for n in nodes if n.get("type") == "vmNode"]
    containers = [n for n in nodes if n.get("type") == "containerNode"]

    total_vcpus = 0
    total_ram_mb = 0
    vm_count = 0
    container_count = 0

    for vm in vms:
        data = vm.get("data", {})
        total_vcpus += data.get("vcpus", 2)
        total_ram_mb += data.get("ram", 4) * 1024
        vm_count += 1

    for ctr in containers:
        data = ctr.get("data", {})
        total_vcpus += data.get("cpus", 1)
        total_ram_mb += data.get("memory", 512)
        container_count += 1

    external_ips = topology.get("externalIps", [])

    return {
        "vm_count": vm_count,
        "container_count": container_count,
        "total_vcpus": total_vcpus,
        "total_ram_mb": total_ram_mb,
        "requested_eips": len(external_ips),
    }


def _get_overcommit_ratios():
    from app.core.config import config

    cpu = getattr(getattr(config, "overcommit", None), "cpu_ratio", 4.0) or 4.0
    ram = getattr(getattr(config, "overcommit", None), "ram_ratio", 1.5) or 1.5
    return float(cpu), float(ram)


def get_allocatable(host: Host) -> tuple[int, int]:
    """Get allocatable vCPUs and RAM for a host with overcommit ratios."""
    cpu_ratio, ram_ratio = _get_overcommit_ratios()
    return int(host.total_vcpus * cpu_ratio), int(host.total_ram_mb * ram_ratio)


def sync_host_capacity(db: Session, host: Host):
    """Recalculate host capacity from all assigned projects."""
    from app.models.project import Project

    projects = (
        db.query(Project)
        .filter(
            Project.host_id == host.id,
            Project.state.in_(
                (
                    "active",
                    "stopped",
                    "deploying",
                    "reconfiguring",
                    "starting",
                    "stopping",
                )
            ),
        )
        .all()
    )
    total_vcpus = 0
    total_ram_mb = 0
    for p in projects:
        reqs = calculate_project_requirements(p.topology or {})
        total_vcpus += reqs["total_vcpus"]
        total_ram_mb += reqs["total_ram_mb"]
    host.used_vcpus = total_vcpus
    host.used_ram_mb = total_ram_mb


def _get_inflight_deploys(host_id: str) -> int:
    """Get count of queued/running deploys targeting a host (from Redis)."""
    try:
        from app.core.redis import get_counter

        return get_counter(f"inflight:deploys:{host_id}")
    except Exception:
        return 0


def record_deploy_start(host_id: str):
    """Increment in-flight deploy counter for a host. Call at placement time."""
    try:
        from app.core.redis import increment_counter

        increment_counter(f"inflight:deploys:{host_id}", ttl=7200)
    except Exception:
        pass


def record_deploy_end(host_id: str):
    """Decrement in-flight deploy counter for a host. Call when deploy completes."""
    try:
        from app.core.redis import decrement_counter

        decrement_counter(f"inflight:deploys:{host_id}")
    except Exception:
        pass


def _check_eip_capacity(db: Session, host: Host, required_eips: int) -> bool:
    """Return True if the host (and its provider) have enough EIP capacity.

    For KubeVirt clusters, host.max_eips reflects the size of the MetalLB
    IPAddressPool (synced at host provision), so the same host-level check
    applies uniformly.
    """
    from app.services.eip_service import get_host_eip_usage

    eip_used = get_host_eip_usage(db, host.id)
    if host.max_eips - eip_used < required_eips:
        return False
    if host.provider_id:
        from app.models.elastic_ip import ElasticIp
        from app.models.provider import Provider as _Prov

        prov = db.query(_Prov).filter_by(id=host.provider_id).first()
        if prov and prov.max_eips is not None:
            total_provider_eips = (
                db.query(func.count(ElasticIp.id))
                .filter(
                    ElasticIp.provider_id == prov.id,
                    ElasticIp.state == "associated",
                )
                .scalar()
            )
            if total_provider_eips + required_eips > prov.max_eips:
                return False
    return True


def _storage_ready_anywhere(db: Session, pattern_disk_ids: list[str]) -> bool:
    """True if any active connected host's provider makes all disks ready."""
    from app.services.pattern_locations import pattern_disks_ready_on_provider

    if not pattern_disk_ids:
        return True
    hosts = (
        db.query(Host)
        .filter(Host.state == "active", Host.agent_status == "connected")
        .all()
    )
    return any(
        pattern_disks_ready_on_provider(db, pattern_disk_ids, h.provider_id)
        for h in hosts
    )


def find_available_host(
    db: Session,
    required_vcpus: int,
    required_ram_mb: int,
    required_eips: int = 0,
    storage_pool_id: str | None = None,
    provider_id: str | None = None,
    pattern_disk_ids: list[str] | None = None,
) -> Host | None:
    """Find the least-loaded active host with enough free capacity (with overcommit).

    Searches across all providers/clusters to spread load. Accounts for both
    DB-committed capacity AND in-flight deploys (queued but not yet reflected
    in DB) to avoid piling jobs onto one cluster.
    """
    query = db.query(Host).filter(
        Host.state == "active",
        Host.agent_status == "connected",
        Host.host_type != "pattern_buffer",
    )
    if storage_pool_id:
        query = query.filter(Host.storage_pool_id == storage_pool_id)
    if provider_id:
        query = query.filter(Host.provider_id == provider_id)

    hosts = query.all()

    # Sync capacity for accurate placement under concurrent load
    for host in hosts:
        sync_host_capacity(db, host)

    candidates = []
    for host in hosts:
        alloc_vcpus, alloc_ram = get_allocatable(host)
        free_vcpus = alloc_vcpus - host.used_vcpus
        free_ram = alloc_ram - host.used_ram_mb
        if free_vcpus >= required_vcpus and free_ram >= required_ram_mb:
            if required_eips > 0 and not _check_eip_capacity(db, host, required_eips):
                continue

            if pattern_disk_ids:
                from app.services.pattern_locations import (
                    pattern_disks_ready_on_provider,
                )

                if not pattern_disks_ready_on_provider(
                    db, pattern_disk_ids, host.provider_id
                ):
                    continue

            inflight = _get_inflight_deploys(host.id)
            candidates.append((host, free_vcpus, free_ram, inflight))

    if not candidates:
        return None

    # Sort by: fewest in-flight deploys first, then most free RAM as tiebreaker.
    # This spreads concurrent deploys across clusters instead of piling onto
    # the one with the most absolute free RAM.
    candidates.sort(key=lambda x: (x[3], -x[2]))
    return candidates[0][0]


def _auto_select_pool(db: Session) -> str | None:
    """Auto-select the best storage pool — the one with the most free RAM across its hosts."""
    from app.models.storage_pool import StoragePool

    pools = db.query(StoragePool).filter(StoragePool.status == "available").all()
    if not pools:
        return None
    if len(pools) == 1:
        return pools[0].id

    best_pool = None
    best_free = -1
    for pool in pools:
        hosts = (
            db.query(Host)
            .filter(
                Host.storage_pool_id == pool.id,
                Host.state == "active",
                Host.agent_status == "connected",
            )
            .all()
        )
        total_free = 0
        for h in hosts:
            _alloc_vcpus, alloc_ram = get_allocatable(h)
            total_free += alloc_ram - h.used_ram_mb
        if total_free > best_free:
            best_free = total_free
            best_pool = pool.id
    return best_pool


def _parse_affinity_groups(
    vm_nodes: list[dict],
) -> tuple[dict[str, list[dict]], list[dict], dict[str, str]]:
    """Parse VM nodes into affinity groups, ungrouped nodes, and anti-affinity map."""
    affinity_groups: dict[str, list[dict]] = {}
    ungrouped: list[dict] = []
    anti_affinity_map: dict[str, str] = {}

    for node in vm_nodes:
        aa = node.get("data", {}).get("separateHost")
        if aa and isinstance(aa, str):
            anti_affinity_map[node["id"]] = aa
        ag = node.get("data", {}).get("affinityGroup")
        if ag:
            affinity_groups.setdefault(ag, []).append(node)
        else:
            ungrouped.append(node)

    return affinity_groups, ungrouped, anti_affinity_map


def _build_placement_units(
    affinity_groups: dict[str, list[dict]], ungrouped: list[dict]
) -> list[dict]:
    """Build placement units from affinity groups and ungrouped VMs."""

    def _group_ram(nodes):
        return sum(n.get("data", {}).get("ram", 4) * 1024 for n in nodes)

    def _group_vcpus(nodes):
        return sum(n.get("data", {}).get("vcpus", 2) for n in nodes)

    units = []
    for ag_nodes in affinity_groups.values():
        units.append(
            {
                "vm_ids": [n["id"] for n in ag_nodes],
                "ram_mb": _group_ram(ag_nodes),
                "vcpus": _group_vcpus(ag_nodes),
            }
        )
    for node in ungrouped:
        units.append(
            {
                "vm_ids": [node["id"]],
                "ram_mb": node.get("data", {}).get("ram", 4) * 1024,
                "vcpus": node.get("data", {}).get("vcpus", 2),
            }
        )

    units.sort(key=lambda u: u["ram_mb"], reverse=True)
    return units


def _prepare_hosts(
    db: Session, pool_id: str | None, provider_id: str | None
) -> tuple[list[Host], dict[str, dict]] | tuple[None, None]:
    """Query and prepare available hosts with capacity tracking."""
    hosts_query = db.query(Host).filter(
        Host.state == "active",
        Host.agent_status == "connected",
        Host.host_type != "pattern_buffer",
        # KubeVirt clusters have no troshkad WireGuard/VXLAN mesh and each host
        # is an entire OCP cluster — they can never be mesh peers for a
        # multi-host deploy, so exclude them from bin-packing.
        Host.host_type != "kubevirt-cluster",
    )
    if pool_id:
        hosts_query = hosts_query.filter(Host.storage_pool_id == pool_id)
    if provider_id:
        hosts_query = hosts_query.filter(Host.provider_id == provider_id)

    available_hosts = hosts_query.all()
    if not available_hosts:
        return None, None

    for h in available_hosts:
        sync_host_capacity(db, h)

    overcommit = 2.0
    host_remaining = {
        h.id: {
            "ram_mb": (h.total_ram_mb or 0) - (h.used_ram_mb or 0),
            "vcpus": int((h.total_vcpus or 0) * overcommit) - (h.used_vcpus or 0),
        }
        for h in available_hosts
    }

    return available_hosts, host_remaining


def _can_place_on_host(
    unit: dict,
    hid: str,
    remaining: dict,
    assignments: dict[str, list[str]],
    anti_affinity_map: dict[str, str],
) -> bool:
    """Check if a unit can be placed on a host considering capacity and anti-affinity."""
    if remaining["ram_mb"] < unit["ram_mb"] or remaining["vcpus"] < unit["vcpus"]:
        return False

    unit_aa_groups = {
        anti_affinity_map[vid] for vid in unit["vm_ids"] if vid in anti_affinity_map
    }
    if unit_aa_groups:
        host_aa_groups = {
            anti_affinity_map[vid]
            for vid in assignments[hid]
            if vid in anti_affinity_map
        }
        if unit_aa_groups & host_aa_groups:
            return False

    return True


def _bin_pack_units(
    units: list[dict],
    available_hosts: list[Host],
    host_remaining: dict[str, dict],
    anti_affinity_map: dict[str, str],
) -> dict[str, list[str]] | None:
    """Bin-pack placement units across hosts respecting anti-affinity."""
    assignments: dict[str, list[str]] = {h.id: [] for h in available_hosts}

    for unit in units:
        sorted_hosts = sorted(
            host_remaining.keys(),
            key=lambda hid: host_remaining[hid]["ram_mb"],
            reverse=True,
        )

        placed = False
        for hid in sorted_hosts:
            remaining = host_remaining[hid]
            if _can_place_on_host(unit, hid, remaining, assignments, anti_affinity_map):
                assignments[hid].extend(unit["vm_ids"])
                remaining["ram_mb"] -= unit["ram_mb"]
                remaining["vcpus"] -= unit["vcpus"]
                placed = True
                break

        if not placed:
            return None

    return {hid: vms for hid, vms in assignments.items() if vms}


def find_multihost_placement(
    db: Session,
    topology: dict,
    pool_id: str | None,
    provider_id: str | None,
) -> dict[str, list[str]] | None:
    """Bin-pack VMs across multiple hosts. Returns {host_id: [vm_node_ids]} or None."""
    vm_nodes = [
        n
        for n in topology.get("nodes", [])
        if n.get("type") in ("vmNode", "containerNode")
    ]
    if not vm_nodes:
        return None

    affinity_groups, ungrouped, anti_affinity_map = _parse_affinity_groups(vm_nodes)
    units = _build_placement_units(affinity_groups, ungrouped)
    available_hosts, host_remaining = _prepare_hosts(db, pool_id, provider_id)

    if not available_hosts or host_remaining is None:
        return None

    return _bin_pack_units(units, available_hosts, host_remaining, anti_affinity_map)


def select_network_host(host_assignments: dict[str, list[str]], topology: dict) -> str:
    """Pick the network host: prefer host with gateway VM, else most VMs."""
    gateway_vm_ids = set()
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode" and node.get("data", {}).get("isGateway"):
            gateway_vm_ids.add(node["id"])

    if gateway_vm_ids:
        for hid, vm_ids in host_assignments.items():
            if gateway_vm_ids & set(vm_ids):
                return hid

    return max(host_assignments, key=lambda hid: len(host_assignments[hid]))


def _has_anti_affinity(topology: dict) -> bool:
    """Check if topology has anti-affinity groups with >1 VM needing separate hosts."""
    aa_groups: dict[str, int] = {}
    for n in topology.get("nodes", []):
        if n.get("type") not in ("vmNode", "containerNode"):
            continue
        aa = n.get("data", {}).get("separateHost")
        if aa and isinstance(aa, str):
            aa_groups[aa] = aa_groups.get(aa, 0) + 1
    return any(count > 1 for count in aa_groups.values())


def _resolve_specified_host(
    db: Session, host_id: str
) -> tuple[Host | None, str | None]:
    """Validate an admin-specified host. Returns (host, error_message)."""
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        return None, f"Host {host_id[:8]} not found"
    if host.state != "active" or host.agent_status != "connected":
        return None, f"Host {host_id[:8]} is not available"
    return host, None


def _select_host(
    db: Session,
    project: Project,
    reqs: dict,
    has_anti_affinity: bool,
    storage_pool_id: str | None,
    host_id: str | None,
    pattern_disk_ids: list[str] | None = None,
) -> tuple[Host | None, str | None, dict | None]:
    """Select a host for the project. Returns (host, storage_pool_id, error_dict)."""
    if host_id:
        host, err = _resolve_specified_host(db, host_id)
        if err or not host:
            return None, storage_pool_id, {"error": err or "Host not found"}
        if not storage_pool_id and host.storage_pool_id:
            storage_pool_id = host.storage_pool_id
        return host, storage_pool_id, None

    if not storage_pool_id:
        storage_pool_id = _auto_select_pool(db)

    host = None
    if not has_anti_affinity:
        host = find_available_host(
            db,
            reqs["total_vcpus"],
            reqs["total_ram_mb"],
            reqs["requested_eips"],
            storage_pool_id=storage_pool_id,
            provider_id=project.provider_id,
            pattern_disk_ids=pattern_disk_ids,
        )
    if not host and not has_anti_affinity and storage_pool_id:
        host = find_available_host(
            db,
            reqs["total_vcpus"],
            reqs["total_ram_mb"],
            reqs["requested_eips"],
            provider_id=project.provider_id,
            pattern_disk_ids=pattern_disk_ids,
        )
    return host, storage_pool_id, None


def _find_pool_for_anti_affinity(db: Session) -> str | None:
    """Find a storage pool with 2+ active hosts for anti-affinity placement."""
    from app.models.storage_pool import StoragePool

    pools = db.query(StoragePool).filter(StoragePool.status == "available").all()
    for pool in pools:
        host_count = (
            db.query(Host)
            .filter(
                Host.storage_pool_id == pool.id,
                Host.state == "active",
                Host.agent_status == "connected",
                Host.host_type != "pattern_buffer",
            )
            .count()
        )
        if host_count >= 2:
            return pool.id
    return None


def _build_multihost_result(
    db: Session, host_assignments: dict[str, list[str]], topology: dict
) -> dict:
    """Build the placement result dict for a multi-host deployment."""
    network_host_id = select_network_host(host_assignments, topology)
    hosts_in_mesh = []
    for hid in host_assignments:
        h = db.query(Host).filter_by(id=hid).first()
        if h:
            hosts_in_mesh.append(h)

    same_pool = (
        len({h.storage_pool_id for h in hosts_in_mesh if h.storage_pool_id}) <= 1
    )
    host_ips = {}
    for h in hosts_in_mesh:
        use_private = same_pool and h.private_ip
        host_ips[h.id] = h.private_ip if use_private else h.ip_address

    vni_map = allocate_vnis_for_project(db, topology)

    return {
        "multi_host": True,
        "host_assignments": host_assignments,
        "network_host_id": network_host_id,
        "host_ips": host_ips,
        "vni_map": vni_map,
    }


def _try_multihost_placement(
    db: Session,
    project: Project,
    storage_pool_id: str | None,
    has_anti_affinity: bool,
) -> dict | None:
    """Try multi-host placement. Returns result dict, error dict, or None if no placement found."""
    multihost_provider = project.provider_id
    multihost_pool = storage_pool_id

    if has_anti_affinity and not multihost_provider and not multihost_pool:
        multihost_pool = _find_pool_for_anti_affinity(db)
        if not multihost_pool:
            return {
                "error": "Anti-affinity requires a provider with multiple hosts. "
                "Select a provider with 2+ hosts or remove anti-affinity."
            }

    assert project.topology is not None
    host_assignments = find_multihost_placement(
        db, project.topology, multihost_pool, multihost_provider
    )
    if not host_assignments:
        return None

    result = _build_multihost_result(db, host_assignments, project.topology)
    logger.info(
        "Placed project %s across %d hosts (network host: %s)",
        project.id,
        len(host_assignments),
        result["network_host_id"][:8],
    )
    return result


def _auto_provision_host(db: Session, reqs: dict) -> tuple[Host | None, dict | None]:
    """Auto-provision a new host. Returns (host, None) on success or (None, error_dict) on failure."""
    logger.info("No host with capacity — auto-provisioning a new one")
    try:
        result = provision_host()
        host = Host(
            id=result["host_id"],
            instance_id=result["instance_id"],
            instance_type=result["instance_type"],
            state="active",
            host_type="shared",
            total_vcpus=result["total_vcpus"],
            total_ram_mb=result["total_ram_mb"],
            max_eips=result.get("max_eips", 0),
            ip_address=result["public_ip"],
            agent_status="disconnected",
        )
        db.add(host)
        db.commit()
        db.refresh(host)
        logger.info("Auto-provisioned host %s (%s)", host.id, host.ip_address)
        return host, None
    except Exception as e:
        logger.exception("Auto-provisioning failed: %s", e)
        return None, {
            "error": f"No host has enough capacity (need {reqs['total_vcpus']} vCPUs, {reqs['total_ram_mb']}MB RAM) and auto-provisioning failed. Check server logs or contact an admin.",
            "required": reqs,
        }


def _finalize_single_host(
    db: Session, project: Project, host: Host, reqs: dict
) -> dict:
    """Finalize single-host placement: allocate VNIs, update state, return result."""
    assert project.topology is not None
    vni_map = allocate_vnis_for_project(db, project.topology)

    all_hosts = db.query(Host).filter(Host.state == "active").all()
    peer_ips = [h.ip_address for h in all_hosts if h.ip_address]
    network_config = build_host_network_config(project.topology, vni_map, peer_ips)

    sync_host_capacity(db, host)
    record_deploy_start(host.id)
    project.host_id = host.id
    project.state = "deploying"
    project.deploy_started_at = datetime.datetime.now(datetime.UTC)
    db.commit()

    logger.info(
        "Placed project %s on host %s (%d vCPUs, %d MB RAM, %d VNIs)",
        project.id,
        host.id,
        reqs["total_vcpus"],
        reqs["total_ram_mb"],
        len(vni_map),
    )

    return {
        "host_id": host.id,
        "host_ip": host.ip_address,
        "requirements": reqs,
        "vni_map": vni_map,
        "network_config": network_config,
    }


def place_project(
    db: Session,
    project: Project,
    storage_pool_id: str | None = None,
    host_id: str | None = None,
) -> dict:
    """Assign a project to a host. Returns placement result."""
    if not project.topology:
        return {"error": "Project has no topology"}

    reqs = calculate_project_requirements(project.topology)
    if reqs["vm_count"] == 0:
        return {"error": "Project has no VMs"}

    from app.services.pattern_locations import pattern_disk_ids_from_topology

    pattern_disk_ids = pattern_disk_ids_from_topology(project.topology)
    if (
        pattern_disk_ids
        and not host_id
        and not _storage_ready_anywhere(db, pattern_disk_ids)
    ):
        return {
            "error": "pattern storage still syncing to central S4 — try again shortly"
        }

    has_anti_affinity = _has_anti_affinity(project.topology)

    host, storage_pool_id, error = _select_host(
        db, project, reqs, has_anti_affinity, storage_pool_id, host_id, pattern_disk_ids
    )
    if error:
        return error

    if not host:
        multihost_result = _try_multihost_placement(
            db, project, storage_pool_id, has_anti_affinity
        )
        if isinstance(multihost_result, dict):
            return multihost_result

        host, provision_error = _auto_provision_host(db, reqs)
        if provision_error:
            return provision_error

    assert host is not None
    return _finalize_single_host(db, project, host, reqs)
