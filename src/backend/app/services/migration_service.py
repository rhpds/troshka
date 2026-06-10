import logging
import threading

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.host import Host
from app.models.project import Project
from app.models.storage_pool import StoragePool

logger = logging.getLogger(__name__)


def validate_migration(db: Session, project_id: str, source_host_id: str, target_host_id: str) -> list[str]:
    errors = []

    project = db.query(Project).get(project_id)
    if not project:
        errors.append("Project not found")
        return errors
    if project.state != "active":
        errors.append(f"Project must be active to migrate (current state: {project.state})")
    if project.host_id != source_host_id:
        errors.append("Project is not on the specified source host")

    source = db.query(Host).get(source_host_id)
    target = db.query(Host).get(target_host_id)
    if not source:
        errors.append("Source host not found")
    if not target:
        errors.append("Target host not found")
    if not source or not target:
        return errors

    if source.storage_pool_id != target.storage_pool_id:
        errors.append("Source and target must be in the same storage pool")
        return errors

    if not source.storage_pool_id:
        errors.append("Hosts must be in a storage pool to migrate")
        return errors

    pool = db.query(StoragePool).get(source.storage_pool_id)
    if pool.mode == "local":
        errors.append("Migration requires shared storage (pool mode is 'local')")

    if target.state != "active":
        errors.append(f"Target host must be active (current state: {target.state})")
    if target.agent_status != "connected":
        errors.append(f"Target host agent must be connected (status: {target.agent_status})")

    # Check target has enough capacity
    from app.services.placement import calculate_project_requirements, get_allocatable, sync_host_capacity
    topology = project.deployed_topology or project.topology or {}
    reqs = calculate_project_requirements(topology)
    sync_host_capacity(db, target)
    alloc_vcpus, alloc_ram = get_allocatable(target)
    free_vcpus = alloc_vcpus - target.used_vcpus
    free_ram = alloc_ram - target.used_ram_mb
    if free_vcpus < reqs["total_vcpus"]:
        errors.append(f"Target host has insufficient CPU ({free_vcpus} free, need {reqs['total_vcpus']})")
    if free_ram < reqs["total_ram_mb"]:
        errors.append(f"Target host has insufficient RAM ({free_ram} MB free, need {reqs['total_ram_mb']} MB)")

    return errors


def migrate_project(project_id: str, source_host_id: str, target_host_id: str):
    t = threading.Thread(
        target=_do_migrate_project,
        args=(project_id, source_host_id, target_host_id),
        daemon=True,
    )
    t.start()


def _do_migrate_project(project_id: str, source_host_id: str, target_host_id: str):
    from app.services.troshkad_client import send_command
    from app.services.deploy_service import (
        _setup_networks_via_troshkad, _teardown_networks_via_troshkad,
        _setup_bmc_via_troshkad, _teardown_bmc_via_troshkad, _extract_bmc_config,
    )

    db = SessionLocal()
    try:
        project = db.query(Project).get(project_id)
        source = db.query(Host).get(source_host_id)
        target = db.query(Host).get(target_host_id)

        project.state = "migrating"
        db.commit()

        topology = project.deployed_topology or project.topology or {}
        vni_map = project.vni_map or {}
        nodes = topology.get("nodes", [])

        # Step 1: Set up networks on target
        network_nodes = [n for n in nodes if n.get("type") == "networkNode"]
        if network_nodes:
            logger.info("Migration %s: setting up networks on target %s", project_id[:8], target_host_id[:8])
            _setup_networks_via_troshkad(target, topology, vni_map, db, project_id)

        # Step 2: Set up BMC on target (if applicable)
        bmc_config = _extract_bmc_config(topology, project_id)
        if bmc_config:
            logger.info("Migration %s: setting up BMC on target", project_id[:8])
            _setup_bmc_via_troshkad(target, project_id, bmc_config)

        # Step 3: Live-migrate each VM in start order
        vm_nodes = [n for n in nodes if n.get("type") == "vmNode"]
        start_order = topology.get("startOrder", [])
        vm_ids_ordered = [s["vmId"] for s in start_order] if start_order else [n["id"] for n in vm_nodes]

        for vm_id in vm_ids_ordered:
            vm_node = next((n for n in vm_nodes if n["id"] == vm_id), None)
            if not vm_node:
                continue

            domain = f"troshka-{project.id[:8]}-{vm_id[:8]}"
            logger.info("Migration %s: migrating VM %s", project_id[:8], domain)

            result = send_command(source, "vm/migrate", {
                "domain": domain,
                "target_host": target.ip_address,
            })
            logger.info("Migration %s: VM %s migrated: %s", project_id[:8], domain, result)

        # Step 4: Tear down source infrastructure
        logger.info("Migration %s: tearing down source %s", project_id[:8], source_host_id[:8])
        if network_nodes:
            _teardown_networks_via_troshkad(source, project_id, vni_map)
        if bmc_config:
            _teardown_bmc_via_troshkad(source, project_id)

        # Step 5: Update DB
        project.host_id = target_host_id
        project.state = "active"
        db.commit()
        logger.info("Migration %s: complete", project_id[:8])

    except Exception as e:
        logger.error("Migration %s failed: %s", project_id[:8], e)
        try:
            project = db.query(Project).get(project_id)
            if project:
                project.state = "error"
                project.deploy_error = f"Migration failed: {e}"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def evacuate_host(host_id: str):
    t = threading.Thread(target=_do_evacuate_host, args=(host_id,), daemon=True)
    t.start()


def _do_evacuate_host(host_id: str):
    db = SessionLocal()
    try:
        host = db.query(Host).get(host_id)

        projects = db.query(Project).filter(
            Project.host_id == host_id,
            Project.state == "active",
        ).all()

        if not projects:
            logger.info("Evacuate %s: no active projects to migrate", host_id[:8])
            return

        logger.info("Evacuate %s: migrating %d projects", host_id[:8], len(projects))

        # Find other hosts in the same pool
        other_hosts = db.query(Host).filter(
            Host.storage_pool_id == host.storage_pool_id,
            Host.id != host_id,
            Host.state == "active",
            Host.agent_status == "connected",
        ).all()

        if not other_hosts:
            logger.error("Evacuate %s: no other hosts available in pool", host_id[:8])
            return

        for project in projects:
            # Simple round-robin target selection
            target = other_hosts[0]  # TODO: implement bin-packing by capacity
            _do_migrate_project(project.id, host_id, target.id)

        host.state = "maintenance"
        db.commit()
        logger.info("Evacuate %s: complete, host set to maintenance", host_id[:8])
    except Exception as e:
        logger.error("Evacuate %s failed: %s", host_id[:8], e)
    finally:
        db.close()
