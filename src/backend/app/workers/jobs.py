"""
Standalone job functions for RQ workers.

These replace closures that were previously defined inside FastAPI route handlers.
Each function accepts only serializable arguments (strings, dicts, bools) and
creates its own DB session internally.
"""

import logging

logger = logging.getLogger(__name__)


def job_start_infra_then_vm(project_id: str, host_id: str, target_vm_id: str):
    """Start infrastructure (networks, EIPs) then a single VM.

    Previously a closure in projects.py that captured p_id, h_id, target_vm_id.
    """
    import json

    from sqlalchemy import text

    from app.core.database import SessionLocal
    from app.models.elastic_ip import ElasticIp
    from app.models.host import Host
    from app.models.project import Project
    from app.services.deploy_service import (
        _get_network_lock,
        _setup_networks_via_troshkad,
        cache_library_images,
    )
    from app.services.eip_service import associate_eip
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job
    from app.services.ws_pubsub import notify_project

    s = SessionLocal()
    try:
        proj = s.query(Project).filter_by(id=project_id).first()
        h = s.query(Host).filter_by(id=host_id).first()
        if not proj or not h:
            return

        topology = proj.topology or {}
        vni_map = proj.vni_map or {}

        project_eips = (
            s.query(ElasticIp).filter_by(project_id=project_id, state="allocated").all()
        )
        for eip in project_eips:
            try:
                associate_eip(s, eip, h)
                for ext_ip in topology.get("externalIps", []):
                    if ext_ip.get("id") == eip.canvas_eip_id:
                        ext_ip["_private_ip"] = eip.private_ip
                        ext_ip["ip"] = eip.public_ip
            except Exception:
                logger.warning("Failed to re-associate EIP %s", eip.public_ip)

        if project_eips:
            s.execute(
                text("UPDATE projects SET topology = :topo WHERE id = :pid"),
                {"topo": json.dumps(topology), "pid": project_id},
            )
            s.commit()
            s.refresh(proj)
            topology = proj.topology or {}

        cache_library_images(topology, h, s)

        if vni_map:
            with _get_network_lock(h.id):
                _setup_networks_via_troshkad(h, topology, vni_map, s, project_id)

        from app.api.projects import _domain_name

        dom = _domain_name(project_id, target_vm_id)
        try:
            job_id = start_job(h, "/vms/start", {"domain_name": dom})
            wait_for_job(h, job_id, timeout=60, poll_interval=2)
            notify_project(
                project_id,
                {
                    "type": "vm-state",
                    "states": {target_vm_id: "running"},
                    "progress": {},
                },
            )
        except TroshkadError as e:
            logger.warning("Failed to start VM %s: %s", dom, e)

        proj.state = "active"
        s.commit()
        notify_project(
            project_id,
            {"type": "project-state", "state": "active", "deploy_error": None},
        )
        logger.info(
            "Infra + VM %s started for project %s",
            target_vm_id[:8],
            project_id[:8],
        )
    except Exception:
        logger.exception("Failed to start infra for project %s", project_id[:8])
        proj = s.query(Project).filter_by(id=project_id).first()
        if proj:
            proj.state = "error"
            s.commit()
    finally:
        s.close()


def job_cache_and_start_vm(project_id: str, host_id: str, vm_id: str):
    """Re-cache missing images then start a single VM.

    Previously a closure in projects.py that captured p_id, h_id, vm_id.
    """
    from app.api.projects import _domain_name
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.services.deploy_service import cache_library_images
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job
    from app.services.ws_pubsub import notify_project

    s = SessionLocal()
    try:
        proj = s.query(Project).filter_by(id=project_id).first()
        h = s.query(Host).filter_by(id=host_id).first()
        if proj and h:
            topo = proj.deployed_topology or proj.topology or {}
            cache_library_images(topo, h, s)
        dom = _domain_name(project_id, vm_id)
        try:
            job_id = start_job(h, "/vms/start", {"domain_name": dom})
            wait_for_job(h, job_id, timeout=60, poll_interval=2)
            notify_project(
                project_id,
                {"type": "vm-state", "states": {vm_id: "running"}, "progress": {}},
            )
        except TroshkadError as e:
            logger.error("Failed to start VM %s: %s", dom, e)
    finally:
        s.close()


def job_redeploy_bg(project_id: str, destroy_ctx: dict | None, old_host_id: str | None):
    """Full project redeploy: destroy old, place new, deploy.

    Previously a closure in projects.py.
    """
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.services.deploy_service import deploy_project_async, destroy_project_sync
    from app.services.gc_service import sync_host_capacity
    from app.services.placement import place_project

    s = SessionLocal()
    try:
        if destroy_ctx:
            destroy_project_sync(destroy_ctx, delete_record=False)
            proj = s.get(Project, project_id)
            if not proj:
                return
            proj.host_id = None
            s.commit()
            h = s.query(Host).filter_by(id=destroy_ctx["host_id"]).first()
            if h:
                sync_host_capacity(s, h)

        proj = s.get(Project, project_id)
        if not proj:
            return
        result = place_project(s, proj, host_id=old_host_id)
        if "error" in result:
            proj.state = "error"
            proj.deploy_error = result["error"]
            s.commit()
            return
        proj.vni_map = result.get("vni_map")
        s.commit()
    finally:
        s.close()

    from app.services.deploy_service import _clear_deploy_cancelled

    _clear_deploy_cancelled(project_id)
    deploy_project_async(project_id)


def job_bulk_deploy_projects(project_ids: list[str]):
    """Place and deploy multiple projects — each deploy enqueued as a separate job."""
    from app.api.patterns import _bulk_deploy_projects

    _bulk_deploy_projects(project_ids)


def job_clean_pattern_cache(pattern_id: str):
    """Remove cached pattern data from all hosts.

    Previously a closure in patterns.py.
    """
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.services.troshkad_client import start_job, wait_for_job

    s = SessionLocal()
    try:
        hosts = s.query(Host).filter(Host.agent_status == "connected").all()
        for h in hosts:
            try:
                job_id = start_job(
                    h,
                    "/cache/remove",
                    {"type": "pattern", "item_id": pattern_id},
                )
                wait_for_job(h, job_id, timeout=30)
            except Exception:
                pass
    finally:
        s.close()


def job_provision_ocpvirt_host(provider_id: str, host_id: str):
    """Provision an OCP Virt host VM."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.provider import Provider
    from app.services.providers import get_provider_driver

    s = SessionLocal()
    try:
        h = s.query(Host).filter_by(id=host_id).first()
        prov = s.query(Provider).filter_by(id=provider_id).first()
        if not h or not prov:
            return
        drv = get_provider_driver(prov)
        result = drv.provision_host(
            provider=prov,
            host_id=host_id,
            instance_type="kubevirt-cluster",
            storage_size_gb=0,
        )
        h.instance_id = result["instance_id"]
        h.instance_type = result["instance_type"]
        h.state = "active"
        h.total_vcpus = result["total_vcpus"]
        h.total_ram_mb = result["total_ram_mb"]
        h.ip_address = result["public_ip"]
        h.private_ip = result.get("private_ip")
        h.agent_status = "connected"
        h.agent_token = prov.get_credentials().get("token", "")
        s.commit()
        logger.info(
            "OCP Virt host %s provisioned for provider %s",
            host_id[:8],
            provider_id[:8],
        )
    except Exception:
        logger.exception("Failed to provision ocpvirt host %s", host_id[:8])
        h = s.query(Host).filter_by(id=host_id).first()
        if h:
            h.state = "error"
            h.agent_status = "provision_failed"
            s.commit()
    finally:
        s.close()


def job_provision_kubevirt(provider_id: str):
    """Provision a KubeVirt virtual host (operator + CRDs)."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.provider import Provider
    from app.services.providers import get_provider_driver

    s = SessionLocal()
    try:
        prov = s.query(Provider).filter_by(id=provider_id).first()
        if not prov:
            return
        # Find the virtual host for this provider
        h = (
            s.query(Host)
            .filter_by(provider_id=provider_id, host_type="kubevirt-cluster")
            .first()
        )
        if not h:
            return
        drv = get_provider_driver(prov)
        result = drv.provision_host(
            provider=prov,
            host_id=h.id,
            instance_type="kubevirt-cluster",
            storage_size_gb=0,
        )
        h.instance_id = result.get("instance_id", h.instance_id)
        h.instance_type = result.get("instance_type", "kubevirt-cluster")
        h.state = "active"
        h.total_vcpus = result.get("total_vcpus", h.total_vcpus)
        h.total_ram_mb = result.get("total_ram_mb", h.total_ram_mb)
        h.ip_address = result.get("public_ip", h.ip_address)
        h.agent_status = "connected"
        s.commit()
        logger.info("KubeVirt provider %s provisioned", provider_id[:8])
    except Exception:
        logger.exception("Failed to provision kubevirt provider %s", provider_id[:8])
        h = (
            s.query(Host)
            .filter_by(provider_id=provider_id, host_type="kubevirt-cluster")
            .first()
        )
        if h:
            h.state = "error"
            h.agent_status = "provision_failed"
            s.commit()
    finally:
        s.close()
