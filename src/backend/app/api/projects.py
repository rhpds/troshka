import logging
import uuid as uuid_mod

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.placement import place_project, calculate_project_requirements
from app.models.host import Host
from app.services.deploy_service import (
    deploy_project_async, stop_project_async, start_project_async, destroy_project_sync,
    diff_topologies, _extract_vms, _find_vm_networks, _find_vm_disks,
    _setup_networks_via_troshkad, _teardown_networks_via_troshkad,
    _create_seed_isos_via_troshkad, _create_vm_disks_via_troshkad, _create_vm_via_troshkad,
    cache_library_images, _vm_dir, _disk_path, _seed_path,
    _setup_pxe_via_troshkad,
)
from app.services.troshkad_client import (
    start_job, wait_for_job, TroshkadError,
    get_vm_state as troshkad_get_vm_state,
    get_vnc_port as troshkad_get_vnc_port,
    get_vm_config as troshkad_get_vm_config,
    reconfigure_vm as troshkad_reconfigure_vm,
    undefine_vm as troshkad_undefine_vm,
)
from app.services.console_proxy import get_or_create_proxy
from app.services.ws_pubsub import notify_project

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("/", response_model=list[ProjectResponse])
def list_projects(
    skip: int = 0,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Project).filter(Project.owner_id == user.id)
    return query.offset(skip).limit(limit).all()


@router.post("/", response_model=ProjectResponse, status_code=201)
def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(Project).filter_by(owner_id=user.id, name=body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"You already have a project named \"{body.name}\"")

    project = Project(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        provider_id=body.provider_id,
        host_type=body.host_type,
        run_timer_hours=body.run_timer_hours,
        lifetime_expires_at=body.lifetime_expires_at,
        poweroff_mode=body.poweroff_mode,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}")
def get_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    # Build response dict from project model
    result = {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "owner_id": project.owner_id,
        "provider_id": project.provider_id,
        "host_type": project.host_type,
        "host_id": project.host_id,
        "state": project.state,
        "topology": project.topology,
        "deployed_topology": project.deployed_topology,
        "vni_map": project.vni_map,
        "deploy_error": project.deploy_error,
        "run_timer_hours": project.run_timer_hours,
        "lifetime_expires_at": project.lifetime_expires_at,
        "poweroff_mode": project.poweroff_mode,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }

    # Include BMC addresses if available
    deployed_topo = project.deployed_topology or {}
    bmc_data = deployed_topo.get("bmc")
    if bmc_data:
        result["bmc"] = bmc_data

    return result


@router.get("/{project_id}/deploy-progress")
def get_deploy_progress(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    from app.services.deploy_service import _deploy_progress
    progress = _deploy_progress.get(project_id)
    return {"state": project.state, "progress": progress}


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/deploy")
def deploy_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state != "draft":
        raise HTTPException(status_code=409, detail=f"Project is {project.state}, not draft")
    if not project.topology:
        raise HTTPException(status_code=400, detail="Project has no topology")

    reqs = calculate_project_requirements(project.topology)
    if reqs["vm_count"] == 0:
        raise HTTPException(status_code=400, detail="Project has no VMs")

    # Validate BMC network has at least one connected provisioner VM
    topology = project.topology or {}
    bmc_network = None
    for node in topology.get("nodes", []):
        if node.get("type") == "networkNode" and node.get("data", {}).get("networkType") == "bmc":
            bmc_network = node
            break
    if bmc_network:
        bmc_edges = [
            e for e in topology.get("edges", [])
            if e.get("source") == bmc_network["id"] or e.get("target") == bmc_network["id"]
        ]
        if not bmc_edges:
            raise HTTPException(
                status_code=400,
                detail="BMC network requires at least one connected VM to act as a provisioner",
            )

    _check_library_items_ready(project.topology, db)

    result = place_project(db, project)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])

    from app.services.troshkad_client import check_disk_usage
    host = db.query(Host).filter_by(id=result["host_id"]).first()
    if host and host.ip_address:
        disk = check_disk_usage(host)
        if disk["used_pct"] >= 90:
            free_gb = disk["free_bytes"] / (1024 ** 3)
            raise HTTPException(status_code=507, detail=f"Host storage is {disk['used_pct']}% full ({free_gb:.1f} GB free). Free space or resize the volume before deploying.")

    # Persist VNI map for stop/start/destroy
    project.vni_map = result.get("vni_map")
    db.commit()

    # Deploy in background
    import threading
    threading.Thread(target=deploy_project_async, args=(project.id,), daemon=True).start()

    return {
        "status": "deploying",
        "host_id": result["host_id"],
        "host_ip": result["host_ip"],
        "requirements": result["requirements"],
    }


@router.post("/{project_id}/stop")
def stop_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state != "active":
        raise HTTPException(status_code=409, detail=f"Project is {project.state}, not active")

    project.state = "stopping"
    db.commit()
    notify_project(project_id, {"type": "project-state", "state": "stopping", "deploy_error": None})

    import threading
    threading.Thread(target=stop_project_async, args=(project.id,), daemon=True).start()

    return {"status": "stopping"}


@router.post("/{project_id}/force-stop")
def force_stop_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.ip_address:
        raise HTTPException(status_code=503, detail="Host not available")

    topo = project.deployed_topology or project.topology or {}
    vms = [n for n in topo.get("nodes", []) if n.get("type") == "vmNode"]
    for vm in vms:
        dom = _domain_name(project_id, vm["id"])
        try:
            job_id = start_job(host, "/vms/destroy", {"domain_name": dom})
            wait_for_job(host, job_id, timeout=30, poll_interval=2)
        except TroshkadError:
            logger.warning("Failed to force-stop VM %s", dom)

    project.state = "stopped"
    db.commit()
    notify_project(project_id, {"type": "project-state", "state": "stopped", "deploy_error": None})
    return {"status": "stopped"}


@router.post("/{project_id}/start")
def start_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state not in ("stopped", "error"):
        raise HTTPException(status_code=409, detail=f"Project is {project.state}, not stopped")

    project.state = "starting"
    db.commit()
    notify_project(project_id, {"type": "project-state", "state": "starting", "deploy_error": None})

    import threading
    threading.Thread(target=start_project_async, args=(project.id,), daemon=True).start()

    return {"status": "starting"}


def _get_project_and_host(project_id: str, user: User, db: Session, check_disk: bool = False):
    """Helper to load project + host with auth and state checks."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state not in ("active", "stopped"):
        raise HTTPException(status_code=409, detail=f"Project is {project.state}, VMs not accessible")
    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.private_key or not host.ip_address:
        raise HTTPException(status_code=503, detail="Host not available")
    if check_disk:
        from app.services.troshkad_client import check_disk_usage
        disk = check_disk_usage(host)
        if disk["used_pct"] >= 90:
            free_gb = disk["free_bytes"] / (1024 ** 3)
            raise HTTPException(status_code=507, detail=f"Host storage is {disk['used_pct']}% full ({free_gb:.1f} GB free). Free space or resize the volume.")
    return project, host


_redeploy_progress: dict[str, dict] = {}


def _check_library_items_ready(topology: dict, db: Session):
    """Ensure all referenced library items are in 'ready' state."""
    from app.models.library import LibraryItem
    for node in topology.get("nodes", []):
        if node.get("type") == "storageNode":
            lib_id = node.get("data", {}).get("libraryItemId")
            if lib_id:
                lib_item = db.query(LibraryItem).filter_by(id=lib_id).first()
                if not lib_item:
                    raise HTTPException(status_code=400, detail=f"Library item not found for '{node['data'].get('name', 'storage')}'")
                if lib_item.state != "ready":
                    raise HTTPException(status_code=400, detail=f"'{lib_item.name}' is still {lib_item.state}. Wait for it to finish.")


def _domain_name(project_id: str, vm_id: str) -> str:
    from app.services.deploy_service import _vm_domain_name
    return _vm_domain_name(project_id, vm_id)


@router.get("/{project_id}/vm-states")
def get_all_vm_states(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get actual running state of all VMs from libvirt."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if not project.host_id:
        return {"states": {}}

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.private_key or not host.ip_address:
        return {"states": {}}

    states = {}
    progress = {}
    for node in project.topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        dom_name = _domain_name(project_id, node["id"])
        if dom_name in _redeploy_progress:
            states[node["id"]] = "redeploying"
            progress[node["id"]] = _redeploy_progress[dom_name]
        else:
            state = troshkad_get_vm_state(host, dom_name)["state"]
            if state == "not_found":
                states[node["id"]] = "not_found"
            elif state == "running":
                states[node["id"]] = "running"
            elif state == "shut_off":
                states[node["id"]] = "stopped"
            else:
                states[node["id"]] = state
    return {"states": states, "progress": progress}


@router.post("/{project_id}/vms/{vm_id}/start")
def start_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)

    if project.state in ("stopped", "starting"):
        import threading
        project.state = "starting"
        db.commit()
        p_id = project.id
        h_id = host.id
        target_vm_id = vm_id

        def _start_infra_then_vm():
            from app.core.database import SessionLocal
            from app.models.project import Project
            from app.models.host import Host as HostModel
            from app.models.elastic_ip import ElasticIp
            from app.services.eip_service import associate_eip
            from app.services.deploy_service import (
                cache_library_images, _setup_networks_via_troshkad,
            )
            import json
            from sqlalchemy import text

            s = SessionLocal()
            try:
                proj = s.query(Project).filter_by(id=p_id).first()
                h = s.query(HostModel).filter_by(id=h_id).first()
                if not proj or not h:
                    return

                topology = proj.topology or {}
                vni_map = proj.vni_map or {}

                # Re-associate EIPs
                project_eips = s.query(ElasticIp).filter_by(project_id=p_id, state="allocated").all()
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
                    s.execute(text("UPDATE projects SET topology = :topo WHERE id = :pid"),
                              {"topo": json.dumps(topology), "pid": p_id})
                    s.commit()
                    s.refresh(proj)
                    topology = proj.topology or {}

                # Re-cache missing images
                cache_library_images(topology, h, s)

                # Recreate bridges and DNAT rules via troshkad
                if vni_map:
                    from app.services.deploy_service import _network_lock
                    with _network_lock:
                        _setup_networks_via_troshkad(h, topology, vni_map, s, p_id)

                # Start only the target VM
                dom = _domain_name(p_id, target_vm_id)
                try:
                    job_id = start_job(h, "/vms/start", {"domain_name": dom})
                    wait_for_job(h, job_id, timeout=60, poll_interval=2)
                    notify_project(p_id, {"type": "vm-state", "states": {target_vm_id: "running"}, "progress": {}})
                except TroshkadError as e:
                    logger.warning("Failed to start VM %s: %s", dom, e)

                proj.state = "active"
                s.commit()
                notify_project(p_id, {"type": "project-state", "state": "active", "deploy_error": None})
                logger.info("Infra + VM %s started for project %s", target_vm_id[:8], p_id[:8])
            except Exception:
                logger.exception("Failed to start infra for project %s", p_id[:8])
                proj = s.query(Project).filter_by(id=p_id).first()
                if proj:
                    proj.state = "error"
                    s.commit()
            finally:
                s.close()

        notify_project(project_id, {"type": "vm-state", "states": {vm_id: "starting"}, "progress": {}})
        threading.Thread(target=_start_infra_then_vm, daemon=True).start()
        return {"action": "start", "success": True, "starting_project": True}

    # Start VM in background — re-cache images if needed, then virsh start
    notify_project(project_id, {"type": "vm-state", "states": {vm_id: "starting"}, "progress": {}})
    import threading
    p_id = project.id
    h_id = host.id

    def _cache_and_start():
        from app.core.database import SessionLocal
        from app.services.deploy_service import cache_library_images
        s = SessionLocal()
        try:
            from app.models.project import Project
            from app.models.host import Host as HostModel
            proj = s.query(Project).filter_by(id=p_id).first()
            h = s.query(HostModel).filter_by(id=h_id).first()
            if proj and h:
                topo = proj.deployed_topology or proj.topology or {}
                cache_library_images(topo, h, s)
            dom = _domain_name(p_id, vm_id)
            try:
                job_id = start_job(h, "/vms/start", {"domain_name": dom})
                wait_for_job(h, job_id, timeout=60, poll_interval=2)
                notify_project(p_id, {"type": "vm-state", "states": {vm_id: "running"}, "progress": {}})
            except TroshkadError as e:
                logger.error("Failed to start VM %s: %s", dom, e)
        finally:
            s.close()

    threading.Thread(target=_cache_and_start, daemon=True).start()
    return {"action": "start", "success": True}


@router.post("/{project_id}/vms/{vm_id}/stop")
def stop_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/stop", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=60, poll_interval=2)
        notify_project(project_id, {"type": "vm-state", "states": {vm_id: "stopped"}, "progress": {}})
        return {"action": "stop", "success": True}
    except TroshkadError as e:
        logger.error("Failed to stop VM %s: %s", dom, e)
        return {"action": "stop", "success": False}


@router.get("/{project_id}/vms/{vm_id}/status")
def get_vm_status(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    vm_info = troshkad_get_vm_state(host, dom)
    return {"state": vm_info["state"], "boot_devs": vm_info.get("boot_devs", [])}


@router.post("/{project_id}/vms/{vm_id}/forcestop")
def forcestop_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/force-off", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=30, poll_interval=2)
        notify_project(project_id, {"type": "vm-state", "states": {vm_id: "stopped"}, "progress": {}})
        return {"action": "forcestop", "success": True}
    except TroshkadError as e:
        logger.error("Failed to force-stop VM %s: %s", dom, e)
        return {"action": "forcestop", "success": False}


@router.post("/{project_id}/vms/{vm_id}/restart")
def restart_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/reboot", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=60, poll_interval=2)
        notify_project(project_id, {"type": "vm-state", "states": {vm_id: "running"}, "progress": {}})
        return {"action": "restart", "success": True}
    except TroshkadError as e:
        logger.error("Failed to restart VM %s: %s", dom, e)
        return {"action": "restart", "success": False}


@router.get("/{project_id}/vms/{vm_id}/console")
def get_vm_console(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    vnc_port = troshkad_get_vnc_port(host, dom)

    if not vnc_port:
        return {"error": "VNC not available"}

    proxy = get_or_create_proxy(dom, host.ip_address, host.private_key, vnc_port)
    if "error" in proxy:
        return {"error": proxy["error"]}

    return {
        "ws_port": proxy["ws_port"],
        "ws_url": proxy["ws_url"],
    }


@router.post("/{project_id}/reconfigure")
def reconfigure_project(
    project_id: str,
    body: dict | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply config changes (boot order, CPU, RAM) without destroying disks."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state not in ("active", "stopped"):
        raise HTTPException(status_code=409, detail=f"Project is {project.state}, cannot reconfigure")
    if not project.host_id:
        raise HTTPException(status_code=400, detail="Project has no active deployment")

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.private_key or not host.ip_address:
        raise HTTPException(status_code=503, detail="Host not available")

    # Validate BMC network has at least one connected provisioner VM
    current = project.topology or {}
    bmc_net = next(
        (n for n in current.get("nodes", [])
         if n.get("type") == "networkNode" and n.get("data", {}).get("networkType") == "bmc"),
        None,
    )
    if bmc_net:
        bmc_edges = [
            e for e in current.get("edges", [])
            if e.get("source") == bmc_net["id"] or e.get("target") == bmc_net["id"]
        ]
        if not bmc_edges:
            raise HTTPException(
                status_code=400,
                detail="BMC network requires at least one connected VM to act as a provisioner",
            )

    # Allocate VNIs for new networks before going async
    deployed = project.deployed_topology or {}
    vni_map = dict(project.vni_map or {})
    diff = diff_topologies(current, deployed) if deployed else {"added_vms": [], "removed_vms": [], "changed_vms": [], "added_networks": [], "removed_networks": [], "has_changes": False}
    if diff["added_networks"]:
        from app.services.vxlan import _get_all_used_vnis, VNI_MIN, VNI_MAX
        used_vnis = _get_all_used_vnis(db) | set(vni_map.values())
        next_vni = VNI_MIN
        for net_node in diff["added_networks"]:
            if net_node.get("data", {}).get("subtype") == "network" and net_node.get("data", {}).get("networkType") != "bmc" and net_node["id"] not in vni_map:
                while next_vni in used_vnis:
                    next_vni += 1
                if next_vni > VNI_MAX:
                    raise HTTPException(status_code=507, detail="VNI pool exhausted")
                vni_map[net_node["id"]] = next_vni
                used_vnis.add(next_vni)
                next_vni += 1
    project.vni_map = vni_map
    project.state = "reconfiguring"
    db.commit()

    restart_vm_ids = set((body or {}).get("restart_vm_ids", []))
    p_id = project.id
    h_id = host.id

    import threading
    def _do_reconfigure():
        from app.core.database import SessionLocal
        from app.services.deploy_service import (
            _vm_domain_name, _resolve_boot_devs,
            _deploy_progress, _create_seed_isos_via_troshkad,
            _create_vm_disks_via_troshkad, _create_vm_via_troshkad,
            _start_vms_via_troshkad,
        )
        from app.services.cloud_init import generate_metadata_service_script

        s = SessionLocal()
        try:
            proj = s.query(Project).filter_by(id=p_id).first()
            h = s.query(Host).filter_by(id=h_id).first()
            if not proj or not h:
                return

            current = proj.topology or {}
            deployed = proj.deployed_topology or {}
            vni_map = dict(proj.vni_map or {})
            diff = diff_topologies(current, deployed) if deployed else {"added_vms": [], "removed_vms": [], "changed_vms": [], "added_networks": [], "removed_networks": [], "has_changes": False}

            errors = []

            # Sync EIPs before networking so DNAT rules have private IPs
            external_ips = current.get("externalIps", [])
            if external_ips:
                try:
                    from app.models.elastic_ip import ElasticIp
                    from app.models.provider import Provider
                    from app.services.eip_service import allocate_eip, associate_eip, sync_security_group_rules
                    provider = s.query(Provider).filter_by(id=proj.provider_id).first() if proj.provider_id else None
                    if not provider and h.provider_id:
                        provider = s.query(Provider).filter_by(id=h.provider_id).first()
                    if provider:
                        for ext_ip in external_ips:
                            canvas_id = ext_ip.get("id", "")
                            existing = s.query(ElasticIp).filter_by(
                                project_id=p_id, canvas_eip_id=canvas_id
                            ).first()
                            eip = existing or allocate_eip(s, provider, p_id, canvas_id)
                            if eip.state != "associated":
                                associate_eip(s, eip, h)
                            ext_ip["ip"] = eip.public_ip
                            ext_ip["_private_ip"] = eip.private_ip
                        import copy, json
                        from sqlalchemy import text
                        new_topo = copy.deepcopy(current)
                        s.execute(
                            text("UPDATE projects SET topology = :topo WHERE id = :pid"),
                            {"topo": json.dumps(new_topo), "pid": p_id},
                        )
                        s.commit()
                        s.refresh(proj)

                        gw_node = next(
                            (n for n in current.get("nodes", [])
                             if n.get("type") == "networkNode" and n.get("data", {}).get("subtype") == "gateway"
                             and n.get("data", {}).get("gatewayMode") == "nat-portforward"),
                            None,
                        )
                        if gw_node:
                            desired_sg = [
                                {"project_id": p_id, "ext_port": int(pf["extPort"]), "protocol": "tcp"}
                                for pf in gw_node.get("data", {}).get("portForwards", [])
                                if pf.get("extPort")
                            ]
                            sync_security_group_rules(s, provider, desired_sg)
                except Exception:
                    logger.exception("EIP sync failed during reconfigure %s", p_id[:8])
                    errors.append("EIP allocation/association failed — check server logs")

            _deploy_progress[p_id] = {"step": "networking", "detail": "configuring"}

            from app.services.deploy_service import _network_lock
            with _network_lock:
                net_result = _setup_networks_via_troshkad(h, current, vni_map, s, p_id)
            if net_result is not True:
                proj.state = "error"
                proj.deploy_error = f"Network setup failed: {net_result}"
                s.commit()
                _deploy_progress.pop(p_id, None)
                return

            # Cache any new library images (ISOs, disk images) before reconfiguring VMs
            _deploy_progress[p_id] = {"step": "downloading", "detail": "0%"}
            def _reconfig_dl_progress(downloaded, total):
                pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
                _deploy_progress[p_id] = {"step": "downloading", "detail": pct}
            cache_library_images(current, h, s, progress_callback=_reconfig_dl_progress)

            # Deploy metadata service via troshkad
            _deploy_progress[p_id] = {"step": "cloud-init", "detail": "deploying metadata service"}
            from app.services.deploy_service import _setup_metadata_via_troshkad
            try:
                _setup_metadata_via_troshkad(h, p_id, current, vni_map)
                logger.info("Reconfigure %s: metadata service deployed", p_id[:8])
            except Exception:
                logger.exception("Reconfigure %s: metadata service deployment failed (non-fatal)", p_id[:8])

            _setup_pxe_via_troshkad(h, current, vni_map, p_id)

            # Create BMC bridge if needed (must exist before VM restart)
            from app.services.deploy_service import _extract_bmc_config
            bmc_config = _extract_bmc_config(current, p_id)
            if bmc_config:
                net_data = bmc_config["bmc_network"]
                cidr = net_data.get("cidr", "192.168.100.0/24")
                try:
                    bj = start_job(h, "/bmc/create-bridge", {
                        "project_id": p_id,
                        "bmc_cidr": cidr,
                        "bmc_gateway_ip": cidr.rsplit(".", 1)[0] + ".1",
                        "vms": [{"bmc_ip": vm["bmc_ip"]} for vm in bmc_config["vms"]],
                    })
                    wait_for_job(h, bj, timeout=30)
                except TroshkadError:
                    logger.warning("Reconfigure %s: BMC bridge creation failed (non-fatal)", p_id[:8])

            vm_dir_path = _vm_dir(p_id)

            for node in diff["removed_vms"]:
                dom = _vm_domain_name(p_id, node["id"])
                troshkad_undefine_vm(h, dom)
                # Remove disk files via troshkad
                try:
                    job_id = start_job(h, "/files/remove", {
                        "paths": [f"{vm_dir_path}/{node['id'][:8]}-{suffix}" for suffix in ["*"]]
                    })
                    wait_for_job(h, job_id, timeout=15)
                except TroshkadError:
                    # Try glob pattern as individual files — files/remove doesn't support globs
                    # Just remove the whole prefix pattern by removing known extensions
                    pass

            vms = _extract_vms(current)
            added_ids = {n["id"] for n in diff["added_vms"]}
            removed_ids = {n["id"] for n in diff["removed_vms"]}
            for vm in vms:
                if vm["node_id"] in added_ids or vm["node_id"] in removed_ids:
                    continue
                dom = _vm_domain_name(p_id, vm["node_id"])
                vm_disks = _find_vm_disks(vm["node_id"], current)
                boot_devs = _resolve_boot_devs(vm, vm_disks, current)
                vm_networks = _find_vm_networks(vm["node_id"], current, vni_map, p_id)
                nics = [{"bridge": n["bridge"], "mac": n["mac"], "model": "virtio"} for n in vm_networks] or None

                # Build map of deployed disk library items for change detection
                dep_disk_libs = {}
                dep_disk_sizes = {}
                dep_vm_node = next((n for n in deployed.get("nodes", []) if n["id"] == vm["node_id"]), None)
                if dep_vm_node:
                    dep_disks = _find_vm_disks(vm["node_id"], deployed)
                    for dd in dep_disks:
                        dep_disk_libs[dd["node_id"]] = dd.get("library_item_id")
                        dep_disk_sizes[dd["node_id"]] = dd.get("size_gb", 0)

                disk_list = []
                cdrom_list = []
                any_disk_changed = False
                needs_library_download = False
                files_to_remove = []
                disks_to_create = []
                disks_to_resize = []
                for d in vm_disks:
                    if d["format"] == "iso":
                        if d.get("library_item_id"):
                            cdrom_list.append(f"/var/lib/troshka/images/{d['library_item_id']}.iso")
                        continue
                    path = _disk_path(p_id, vm["node_id"], d["node_id"], d["format"])
                    disk_list.append({"path": path, "format": d["format"], "bus": d["bus"]})
                    old_lib = dep_disk_libs.get(d["node_id"])
                    new_lib = d.get("library_item_id")
                    image_changed = old_lib != new_lib and (old_lib or new_lib)
                    old_size = dep_disk_sizes.get(d["node_id"], 0)
                    size_grew = d["size_gb"] > old_size and old_size > 0
                    is_new_disk = d["node_id"] not in dep_disk_libs and d["node_id"] not in dep_disk_sizes
                    if image_changed or size_grew or is_new_disk:
                        any_disk_changed = True
                    if image_changed:
                        files_to_remove.append(path)
                    backing = None
                    if d.get("source") == "library" and d.get("library_item_id"):
                        needs_library_download = True
                        backing = f"/var/lib/troshka/images/{d['library_item_id']}.{d['format']}"
                    elif d.get("source") == "pattern" and d.get("patternId"):
                        backing = f"/var/lib/troshka/cache/patterns/{d['patternId']}/{d['patternDiskId']}.{d['format']}"
                    disks_to_create.append({"path": path, "size_gb": d["size_gb"], "format": d["format"], "backing_file": backing})
                    if size_grew and not image_changed:
                        disks_to_resize.append({"path": path, "new_size_gb": d["size_gb"]})

                if vm.get("cloud_init"):
                    cdrom_list.append(_seed_path(p_id, vm["node_id"]))

                if any_disk_changed:
                    if needs_library_download:
                        _deploy_progress[p_id] = {"step": "checking images", "detail": ""}
                        cache_library_images(current, h, s)
                    # Remove changed disk files
                    if files_to_remove:
                        try:
                            job_id = start_job(h, "/files/remove", {"paths": files_to_remove})
                            wait_for_job(h, job_id, timeout=30)
                        except TroshkadError as e:
                            logger.warning("Failed to remove old disk files: %s", e)
                    # Create new disks
                    for dc in disks_to_create:
                        params = {"path": dc["path"], "size_gb": dc["size_gb"], "format": dc["format"]}
                        if dc["backing_file"]:
                            params["backing_file"] = dc["backing_file"]
                        try:
                            job_id = start_job(h, "/disks/create", params)
                            wait_for_job(h, job_id, timeout=300)
                        except TroshkadError as e:
                            logger.warning("Failed to create disk %s: %s", dc["path"], e)
                    # Resize disks
                    for dr in disks_to_resize:
                        try:
                            job_id = start_job(h, "/disks/resize", dr)
                            wait_for_job(h, job_id, timeout=60)
                        except TroshkadError as e:
                            logger.warning("Failed to resize disk %s: %s", dr["path"], e)

                current_cfg = troshkad_get_vm_config(h, dom)
                if not current_cfg:
                    vm_node = next((n for n in current.get("nodes", []) if n["id"] == vm["node_id"]), None)
                    if vm_node:
                        diff["added_vms"].append(vm_node)
                    continue

                desired_nics = [{"bridge": n["bridge"], "mac": n["mac"]} for n in vm_networks] if vm_networks else []
                desired_disks = [d["path"] for d in disk_list]
                if (
                    current_cfg["boot_devs"] == boot_devs and
                    current_cfg["vcpus"] == vm["vcpus"] and
                    current_cfg["ram_mb"] == vm["ram_gb"] * 1024 and
                    current_cfg["nics"] == desired_nics and
                    current_cfg["disks"] == desired_disks and
                    sorted(current_cfg.get("cdroms", [])) == sorted(cdrom_list)
                ):
                    continue

                _deploy_progress[p_id] = {"step": "reconfiguring", "detail": vm["name"]}
                needs_restart = vm["node_id"] in restart_vm_ids or current_cfg["boot_devs"] != boot_devs or current_cfg["vcpus"] != vm["vcpus"] or current_cfg["ram_mb"] != vm["ram_gb"] * 1024 or current_cfg["nics"] != desired_nics or current_cfg["disks"] != desired_disks
                try:
                    troshkad_reconfigure_vm(h, dom, boot_devs=boot_devs, vcpus=vm["vcpus"], ram_mb=vm["ram_gb"] * 1024, nics=nics, disks=disk_list, cdroms=cdrom_list, restart=needs_restart)
                except TroshkadError as e:
                    errors.append(f"Failed to reconfigure {dom}: {e}")

            if diff["added_vms"]:
                _deploy_progress[p_id] = {"step": "downloading", "detail": "0%"}
                def _progress(downloaded, total):
                    pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
                    _deploy_progress[p_id] = {"step": "downloading", "detail": pct}
                cache_library_images(current, h, s, progress_callback=_progress)
                _create_seed_isos_via_troshkad(h, p_id, current)
                _deploy_progress[p_id] = {"step": "creating", "detail": "VMs"}
                for vm_node in diff["added_vms"]:
                    vd = vm_node.get("data", {})
                    vm_data = {
                        "node_id": vm_node["id"],
                        "name": vd.get("name", "vm"),
                        "vcpus": vd.get("vcpus", 2),
                        "ram_gb": vd.get("ram", 4),
                        "cloud_init": vd.get("cloudInit", False),
                        "boot_devices": vd.get("bootDevices"),
                        "firmware": vd.get("firmware", "bios"),
                        "secure_boot": vd.get("secureBoot", False),
                    }
                    vm_disks_add = _find_vm_disks(vm_node["id"], current)
                    try:
                        _create_vm_disks_via_troshkad(h, p_id, vm_data, vm_disks_add)
                        _create_vm_via_troshkad(h, p_id, vm_data, current, vni_map)
                        # Start if auto-start not disabled
                        no_auto_start = {e["vmId"] for e in current.get("startOrder", []) if e.get("autoStart") is False}
                        if vm_node["id"] not in no_auto_start:
                            vm_name = _vm_domain_name(p_id, vm_node["id"])
                            job_id = start_job(h, "/vms/start", {"domain_name": vm_name})
                            wait_for_job(h, job_id, timeout=60)
                    except (TroshkadError, RuntimeError) as e:
                        errors.append(f"Failed to add VM {vm_node['id'][:8]}: {e}")

            from app.services.placement import sync_host_capacity
            sync_host_capacity(s, h)

            # BMC setup/teardown during reconfigure
            from app.services.deploy_service import _extract_bmc_config, _setup_bmc_via_troshkad, _teardown_bmc_via_troshkad
            bmc_config = _extract_bmc_config(current, p_id)
            deployed_had_bmc = any(
                n.get("type") == "networkNode" and n.get("data", {}).get("networkType") == "bmc"
                for n in deployed.get("nodes", [])
            )
            if deployed_had_bmc:
                _teardown_bmc_via_troshkad(h, p_id)
            if bmc_config:
                bmc_result = _setup_bmc_via_troshkad(h, p_id, bmc_config)
                if bmc_result is not True:
                    errors.append(f"BMC setup failed: {bmc_result}")

            s.refresh(proj)
            final_topo = proj.topology or {}

            import copy
            proj.state = "active"
            if not errors:
                # Store BMC addresses in deployed topology
                deployed_topo = copy.deepcopy(final_topo)
                if bmc_config:
                    deployed_topo["bmc"] = {
                        "username": bmc_config["bmc_network"].get("bmcUsername", "admin"),
                        "password": bmc_config["bmc_network"].get("bmcPassword", "password"),
                        "vms": {
                            vm["node_id"]: {
                                "ip": vm["bmc_ip"],
                                "redfish_url": f"redfish-virtualmedia://{vm['bmc_ip']}:8000/redfish/v1/Systems/{vm['domain_name']}",
                                "ipmi_address": f"{vm['bmc_ip']}:623",
                            }
                            for vm in bmc_config["vms"]
                        },
                    }
                proj.deployed_topology = deployed_topo
                proj.deploy_error = None
            else:
                proj.deploy_error = "\n".join(errors)
            s.commit()
            _deploy_progress.pop(p_id, None)
            logger.info("Reconfigure %s complete%s", p_id[:8], f" with errors: {errors}" if errors else "")
        except Exception:
            logger.exception("Reconfigure %s failed", p_id[:8])
            proj = s.query(Project).filter_by(id=p_id).first()
            if proj:
                proj.state = "error"
                s.commit()
            _deploy_progress.pop(p_id, None)
        finally:
            s.close()

    threading.Thread(target=_do_reconfigure, daemon=True).start()
    return {"status": "reconfiguring"}


@router.post("/{project_id}/vms/{vm_id}/redeploy")
def redeploy_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Destroy and recreate a single VM in a background thread."""
    project, host = _get_project_and_host(project_id, user, db, check_disk=True)
    _check_library_items_ready(project.topology, db)

    p_id = project.id
    host_id = host.id
    target_vm_id = vm_id

    import threading
    def _do_redeploy():
        from app.core.database import SessionLocal
        from app.services.deploy_service import (
            _vm_domain_name, _deploy_progress,
            _create_seed_isos_via_troshkad,
            _create_vm_disks_via_troshkad, _create_vm_via_troshkad,
        )

        s = SessionLocal()
        try:
            proj = s.query(Project).filter_by(id=p_id).first()
            h = s.query(Host).filter_by(id=host_id).first()
            if not proj or not h:
                return

            dom = _vm_domain_name(p_id, target_vm_id)
            vm_dir_path = _vm_dir(p_id)
            topology = proj.topology
            vni_map = proj.vni_map or {}

            was_running = troshkad_get_vm_state(h, dom)["state"] == "running"
            troshkad_undefine_vm(h, dom, remove_storage=False)

            _redeploy_progress[dom] = {"step": "preparing", "detail": ""}
            # Remove old disk files via troshkad
            # Build list of known files for this VM
            vm_disks_to_remove = _find_vm_disks(target_vm_id, topology)
            paths_to_remove = []
            for d in vm_disks_to_remove:
                if d["format"] != "iso":
                    paths_to_remove.append(_disk_path(p_id, target_vm_id, d["node_id"], d["format"]))
            paths_to_remove.append(_seed_path(p_id, target_vm_id))
            try:
                job_id = start_job(h, "/files/remove", {"paths": paths_to_remove})
                wait_for_job(h, job_id, timeout=15)
            except TroshkadError as e:
                logger.warning("Redeploy %s: failed to remove old files: %s", dom, e)

            vm_node = next((n for n in topology.get("nodes", []) if n["id"] == target_vm_id and n.get("type") == "vmNode"), None)
            if not vm_node:
                logger.warning("Redeploy %s: node not found in topology", target_vm_id[:8])
                _redeploy_progress.pop(dom, None)
                return

            edges = topology.get("edges", [])
            vm_connected_ids = set()
            for edge in edges:
                src, tgt = edge.get("source"), edge.get("target")
                if src == target_vm_id:
                    vm_connected_ids.add(tgt)
                elif tgt == target_vm_id:
                    vm_connected_ids.add(src)
            vm_topo = {"nodes": [n for n in topology.get("nodes", []) if n["id"] in vm_connected_ids]}

            _redeploy_progress[dom] = {"step": "downloading", "detail": "0%"}
            def _progress(downloaded, total):
                pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
                _redeploy_progress[dom] = {"step": "downloading", "detail": pct}
            cache_library_images(vm_topo, h, s, progress_callback=_progress)

            _setup_pxe_via_troshkad(h, topology, vni_map, p_id)

            _redeploy_progress[dom] = {"step": "creating", "detail": "cloud-init seed ISO"}
            _create_seed_isos_via_troshkad(h, p_id, topology)

            _redeploy_progress[dom] = {"step": "creating", "detail": "VM definition"}
            vdata = vm_node.get("data", {})
            vm_data = {
                "node_id": vm_node["id"],
                "name": vdata.get("name", "vm"),
                "vcpus": vdata.get("vcpus", 2),
                "ram_gb": vdata.get("ram", 4),
                "cloud_init": vdata.get("cloudInit", False),
                "boot_devices": vdata.get("bootDevices"),
                "firmware": vdata.get("firmware", "bios"),
                "secure_boot": vdata.get("secureBoot", False),
            }
            vm_disks = _find_vm_disks(target_vm_id, topology)
            _create_vm_disks_via_troshkad(h, p_id, vm_data, vm_disks)
            _create_vm_via_troshkad(h, p_id, vm_data, topology, vni_map)

            if was_running:
                try:
                    job_id = start_job(h, "/vms/start", {"domain_name": dom})
                    wait_for_job(h, job_id, timeout=60)
                except TroshkadError as e:
                    logger.warning("Failed to start VM %s after redeploy: %s", dom, e)

            _redeploy_progress[dom] = {"step": "starting", "detail": ""}
            proj.deployed_topology = topology
            s.commit()
            _redeploy_progress.pop(dom, None)
            logger.info("Redeploy %s complete", dom)
        except Exception:
            logger.exception("Redeploy %s failed", target_vm_id[:8])
            _redeploy_progress.pop(_vm_domain_name(p_id, target_vm_id), None)
        finally:
            s.close()

    threading.Thread(target=_do_redeploy, daemon=True).start()
    return {"status": "redeploying"}


@router.post("/{project_id}/vms/{vm_id}/cancel-redeploy")
def cancel_redeploy(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Cancel a stuck redeploy by clearing the progress tracker."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    dom = _domain_name(project_id, vm_id)
    _redeploy_progress.pop(dom, None)
    return {"status": "cancelled"}


@router.post("/{project_id}/redeploy")
def redeploy_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Destroy existing infrastructure and redeploy with current topology."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state not in ("active", "stopped", "error"):
        raise HTTPException(status_code=409, detail=f"Project is {project.state}, cannot redeploy")

    _check_library_items_ready(project.topology, db)

    # Destroy existing and release capacity
    if project.host_id:
        old_host_id = project.host_id
        old_host = db.query(Host).filter_by(id=old_host_id).first()
        if not old_host or not old_host.ip_address:
            raise HTTPException(status_code=503, detail="Host not reachable — cannot destroy existing VMs. Stop the project first or wait for the host to come online.")
        destroy_project_sync(project.id)
        project.host_id = None
        db.commit()
        from app.services.gc_service import sync_host_capacity
        sync_host_capacity(db, old_host)

    # Reset for fresh deploy
    project.state = "deploying"
    project.host_id = None
    project.vni_map = None
    project.deploy_error = None
    db.commit()

    # Now deploy again
    if not project.topology:
        raise HTTPException(status_code=400, detail="Project has no topology")

    reqs = calculate_project_requirements(project.topology)
    if reqs["vm_count"] == 0:
        raise HTTPException(status_code=400, detail="Project has no VMs")

    result = place_project(db, project)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])

    project.vni_map = result.get("vni_map")
    db.commit()

    import threading
    threading.Thread(target=deploy_project_async, args=(project.id,), daemon=True).start()

    return {
        "status": "deploying",
        "host_id": result["host_id"],
        "host_ip": result["host_ip"],
        "requirements": result["requirements"],
    }


@router.post("/{project_id}/undeploy")
def undeploy_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Destroy all infrastructure and reset project to draft."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    if project.host_id:
        destroy_project_sync(project.id)

    project.state = "draft"
    project.host_id = None
    project.vni_map = None
    project.deploy_error = None
    db.commit()

    return {"status": "draft"}


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    notify_project(project_id, {"type": "project-deleted"})

    # Release EIPs before deleting DB record (delete cascades null the FK)
    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import release_eip
    project_eips = db.query(ElasticIp).filter_by(project_id=project_id).all()
    for eip in project_eips:
        try:
            release_eip(db, eip)
        except Exception:
            logger.warning("Failed to release EIP %s on delete", eip.public_ip)

    # Clean up infrastructure in background, delete DB record immediately
    if project.host_id and project.state in ("active", "stopped", "error"):
        import threading
        p_id = project.id
        threading.Thread(target=destroy_project_sync, args=(p_id,), daemon=True).start()

    db.delete(project)
    db.commit()


class ImportVMRequest(PydanticBaseModel):
    snapshot_id: str
    position_x: float = 100.0
    position_y: float = 100.0


@router.post("/{project_id}/import-vm", response_model=ProjectResponse)
def import_vm_from_snapshot(
    project_id: str,
    body: ImportVMRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    from app.models.library import Library, LibraryItem

    item = (
        db.query(LibraryItem)
        .join(Library, LibraryItem.library_id == Library.id)
        .filter(
            LibraryItem.id == body.snapshot_id,
            LibraryItem.type == "snapshot",
            Library.owner_id == user.id,
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    vm_config = item.vm_config or {}
    vm_id = str(uuid_mod.uuid4())

    import random

    def _gen_mac():
        return "52:54:00:%02x:%02x:%02x" % (
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
        )

    vm_node = {
        "id": vm_id,
        "type": "vmNode",
        "position": {"x": body.position_x, "y": body.position_y},
        "data": {
            "label": item.name,
            "name": item.name,
            "vcpus": vm_config.get("vcpus", 2),
            "ram": vm_config.get("ram", 4096),
            "os": vm_config.get("os", ""),
            "status": "stopped",
            "icon": "\U0001f5a5",
            "nics": [
                {**nic, "id": f"nic-{uuid_mod.uuid4()}", "mac": _gen_mac()}
                for nic in vm_config.get("nics", [])
            ],
            "diskControllers": [
                {**dc, "id": f"dp-{uuid_mod.uuid4()}"}
                for dc in vm_config.get("diskControllers", [])
            ],
            "bootMethod": vm_config.get("bootMethod"),
            "cloudInit": vm_config.get("cloudInit"),
            "consoleType": vm_config.get("consoleType"),
            "autoStart": vm_config.get("autoStart"),
            "snapshotItemId": item.id,
        },
    }

    topology = dict(project.topology or {"nodes": [], "edges": []})
    topology["nodes"] = list(topology.get("nodes", []))
    topology["edges"] = list(topology.get("edges", []))

    existing_names = {n.get("data", {}).get("name", "") for n in topology["nodes"]}

    def _unique_name(base: str) -> str:
        if base not in existing_names:
            existing_names.add(base)
            return base
        i = 1
        while f"{base}-{i}" in existing_names:
            i += 1
        name = f"{base}-{i}"
        existing_names.add(name)
        return name

    topology["nodes"].append(vm_node)

    disks = vm_config.get("disks", [])
    dc_list = vm_node["data"]["diskControllers"]
    boot_devices = []

    for idx, disk_info in enumerate(disks):
        disk_id = str(uuid_mod.uuid4())
        disk_name = _unique_name(disk_info.get("name", "disk"))
        disk_node = {
            "id": disk_id,
            "type": "storageNode",
            "position": {"x": body.position_x - 250, "y": body.position_y + idx * 150},
            "data": {
                "label": disk_name,
                "name": disk_name,
                "size": disk_info.get("size", 20),
                "format": disk_info.get("format", "qcow2"),
                "source": "snapshot",
                "snapshotItemId": item.id,
                "icon": "\U0001f6e2" if disk_info.get("format") != "iso" else "\U0001f4bf",
            },
        }
        topology["nodes"].append(disk_node)

        target_handle = ""
        if dc_list and idx < len(dc_list):
            target_handle = f"dp-{dc_list[idx]['id']}-left"

        edge = {
            "id": f"xy-edge__{disk_id}right-{vm_id}{target_handle}",
            "source": disk_id,
            "target": vm_id,
            "sourceHandle": "right",
            "targetHandle": target_handle or None,
            "type": "smoothstep",
            "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
        }
        topology["edges"].append(edge)
        boot_devices.append(disk_id)

    if boot_devices:
        vm_node["data"]["bootDevices"] = boot_devices

    networks_info = vm_config.get("networks", [])
    nic_list = vm_node["data"]["nics"]
    canvas_networks = {
        n.get("data", {}).get("name", ""): n
        for n in topology["nodes"]
        if n.get("type") == "networkNode"
    }

    for net_info in networks_info:
        net_name = net_info.get("name", "")
        matching_net = canvas_networks.get(net_name)
        if not matching_net:
            continue
        if not nic_list:
            continue
        nic = nic_list[0]
        src_handle = f"nic-{nic['id']}-top"
        edge = {
            "id": f"xy-edge__{vm_id}{src_handle}-{matching_net['id']}bottom",
            "source": vm_id,
            "target": matching_net["id"],
            "sourceHandle": src_handle,
            "targetHandle": "bottom",
            "type": "smoothstep",
            "style": {"stroke": "rgba(56,189,248,0.6)", "strokeWidth": 2, "strokeDasharray": "6 4"},
        }
        topology["edges"].append(edge)
        nic_list = nic_list[1:]

    project.topology = topology
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "topology")
    db.commit()
    db.refresh(project)
    return project
