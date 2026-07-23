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


def find_available_host(
    db: Session,
    required_vcpus: int,
    required_ram_mb: int,
    required_eips: int = 0,
    storage_pool_id: str | None = None,
) -> Host | None:
    """Find the least-loaded active host with enough free capacity (with overcommit).
    Syncs capacity from DB first to handle concurrent deployments."""
    query = db.query(Host).filter(
        Host.state == "active",
        Host.agent_status == "connected",
        Host.host_type != "pattern_buffer",
    )
    if storage_pool_id:
        query = query.filter(Host.storage_pool_id == storage_pool_id)

    hosts = query.all()

    # Sync capacity for accurate placement under concurrent load
    for host in hosts:
        sync_host_capacity(db, host)

    # Sort by most free capacity (least loaded first)
    candidates = []
    for host in hosts:
        alloc_vcpus, alloc_ram = get_allocatable(host)
        free_vcpus = alloc_vcpus - host.used_vcpus
        free_ram = alloc_ram - host.used_ram_mb
        if free_vcpus >= required_vcpus and free_ram >= required_ram_mb:
            if required_eips > 0:
                from app.services.eip_service import get_host_eip_usage

                eip_used = get_host_eip_usage(db, host.id)
                if host.max_eips - eip_used < required_eips:
                    continue
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
                            continue
            candidates.append((host, free_vcpus, free_ram))

    if not candidates:
        return None

    # Pick the host with the most free RAM (spreads load across hosts)
    candidates.sort(key=lambda x: x[2], reverse=True)
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
            alloc_vcpus, alloc_ram = get_allocatable(h)
            total_free += alloc_ram - h.used_ram_mb
        if total_free > best_free:
            best_free = total_free
            best_pool = pool.id
    return best_pool


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

    # Admin-specified host override
    if host_id:
        host = db.query(Host).filter_by(id=host_id).first()
        if not host:
            return {"error": f"Host {host_id[:8]} not found"}
        if host.state != "active" or host.agent_status != "connected":
            return {"error": f"Host {host_id[:8]} is not available"}
        if not storage_pool_id and host.storage_pool_id:
            storage_pool_id = host.storage_pool_id
    else:
        # Auto-select pool if not specified
        if not storage_pool_id:
            storage_pool_id = _auto_select_pool(db)

        host = find_available_host(
            db,
            reqs["total_vcpus"],
            reqs["total_ram_mb"],
            reqs["requested_eips"],
            storage_pool_id=storage_pool_id,
        )
        if not host and storage_pool_id:
            host = find_available_host(
                db,
                reqs["total_vcpus"],
                reqs["total_ram_mb"],
                reqs["requested_eips"],
            )
    if not host:
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
        except Exception as e:
            logger.exception("Auto-provisioning failed: %s", e)
            return {
                "error": f"No host has enough capacity (need {reqs['total_vcpus']} vCPUs, {reqs['total_ram_mb']}MB RAM) and auto-provisioning failed. Check server logs or contact an admin.",
                "required": reqs,
            }

    # Allocate VNIs for project networks
    vni_map = allocate_vnis_for_project(db, project.topology)

    # Get all host IPs for VXLAN mesh
    all_hosts = db.query(Host).filter(Host.state == "active").all()
    peer_ips = [h.ip_address for h in all_hosts if h.ip_address]

    # Build network config for the agent
    network_config = build_host_network_config(project.topology, vni_map, peer_ips)

    # Update host capacity
    sync_host_capacity(db, host)
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
