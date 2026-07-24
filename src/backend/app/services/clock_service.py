"""Clock backdating service — compute offsets and push time to running VMs."""

import datetime
import logging

logger = logging.getLogger(__name__)


def compute_clock_offset(clock_target: datetime.datetime) -> int:
    """Compute seconds offset from current UTC to the target datetime."""
    now = datetime.datetime.now(datetime.UTC)
    if clock_target.tzinfo is None:
        clock_target = clock_target.replace(tzinfo=datetime.UTC)
    return int((clock_target - now).total_seconds())


def adjust_clocks_async(project_id: str):
    """Enqueue clock adjustment for all running VMs in a project."""
    from app.core.redis import enqueue_job

    enqueue_job(_adjust_clocks, project_id, queue_name="default")


def _adjust_clocks(project_id: str):
    """Push updated clock to all VMs in a project."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "active":
            return

        host = (
            s.query(Host).filter_by(id=project.host_id).first()
            if project.host_id
            else None
        )
        if not host:
            return

        topology = project.deployed_topology or project.topology or {}
        nodes = topology.get("nodes", [])
        vm_nodes = [n for n in nodes if n.get("type") == "vmNode"]

        if project.clock_target:
            offset_seconds = compute_clock_offset(project.clock_target)
            target_epoch = int(project.clock_target.timestamp())
        else:
            offset_seconds = None
            target_epoch = None

        # Process gateway first (it's the NTP server), then other VMs
        gateway_nodes = [n for n in nodes if n.get("type") == "gatewayNode"]
        gateway_domain = None
        if gateway_nodes:
            gw = gateway_nodes[0]
            gateway_domain = f"troshka-{project_id[:8]}-{gw['id'][:8]}"
            _push_clock_to_vm(host, gateway_domain, offset_seconds, target_epoch)

        for node in vm_nodes:
            domain = f"troshka-{project_id[:8]}-{node['id'][:8]}"
            if domain == gateway_domain:
                continue
            _push_clock_to_vm(host, domain, offset_seconds, target_epoch)

        logger.info(
            "Clock adjustment complete for project %s (%s VMs)",
            project_id[:8],
            len(vm_nodes),
        )
    except Exception:
        logger.exception("Clock adjustment failed for project %s", project_id[:8])
    finally:
        s.close()


def _push_clock_to_vm(host, domain_name, offset_seconds, target_epoch):
    """Update a single VM's clock: XML + live push."""
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    try:
        job_id = start_job(
            host,
            "/vms/set-clock",
            {
                "domain_name": domain_name,
                "offset_seconds": offset_seconds,
                "target_epoch": target_epoch,
            },
        )
        wait_for_job(host, job_id, timeout=30)
    except TroshkadError as e:
        logger.warning("Clock push failed for %s: %s", domain_name, e)
