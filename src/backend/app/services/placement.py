"""
Placement service — assigns a project's VMs to available hosts.

Called when a user clicks Deploy. Finds a host with enough capacity
for the project's VMs, or fails if no host has room.
"""
import logging

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

    total_vcpus = 0
    total_ram_mb = 0
    vm_count = 0

    for vm in vms:
        data = vm.get("data", {})
        total_vcpus += data.get("vcpus", 2)
        total_ram_mb += data.get("ram", 4) * 1024
        vm_count += 1

    return {
        "vm_count": vm_count,
        "total_vcpus": total_vcpus,
        "total_ram_mb": total_ram_mb,
    }


def find_available_host(db: Session, required_vcpus: int, required_ram_mb: int) -> Host | None:
    """Find an active host with enough free capacity."""
    hosts = db.query(Host).filter(
        Host.state == "active",
        Host.agent_status == "connected",
    ).all()

    for host in hosts:
        free_vcpus = host.total_vcpus - host.used_vcpus
        free_ram = host.total_ram_mb - host.used_ram_mb
        if free_vcpus >= required_vcpus and free_ram >= required_ram_mb:
            return host

    return None


def place_project(db: Session, project: Project) -> dict:
    """Assign a project to a host. Returns placement result."""
    if not project.topology:
        return {"error": "Project has no topology"}

    reqs = calculate_project_requirements(project.topology)
    if reqs["vm_count"] == 0:
        return {"error": "Project has no VMs"}

    host = find_available_host(db, reqs["total_vcpus"], reqs["total_ram_mb"])
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
                ip_address=result["public_ip"],
                agent_status="disconnected",
            )
            db.add(host)
            db.commit()
            db.refresh(host)
            logger.info("Auto-provisioned host %s (%s)", host.id, host.ip_address)
        except Exception as e:
            return {
                "error": f"No host available and auto-provision failed: {e}",
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
    host.used_vcpus += reqs["total_vcpus"]
    host.used_ram_mb += reqs["total_ram_mb"]
    project.host_id = host.id
    project.state = "deploying"
    db.commit()

    logger.info(
        "Placed project %s on host %s (%d vCPUs, %d MB RAM, %d VNIs)",
        project.id, host.id, reqs["total_vcpus"], reqs["total_ram_mb"], len(vni_map),
    )

    return {
        "host_id": host.id,
        "host_ip": host.ip_address,
        "requirements": reqs,
        "vni_map": vni_map,
        "network_config": network_config,
    }
