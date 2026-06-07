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
from app.services.deploy_service import deploy_project_async, stop_project_async, start_project_async, destroy_project_sync, run_ssh_script, diff_topologies, generate_incremental_script, _extract_vms, _find_vm_networks, _find_vm_disks
from app.services.vxlan import generate_setup_script
from app.services import libvirt_mgr
from app.services.console_proxy import get_or_create_proxy

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


@router.get("/{project_id}", response_model=ProjectResponse)
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
    return project


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

    _check_library_items_ready(project.topology, db)

    result = place_project(db, project)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])

    from app.services.deploy_service import check_host_disk_space
    host = db.query(Host).filter_by(id=result["host_id"]).first()
    if host and host.ip_address and host.private_key:
        disk = check_host_disk_space(host.ip_address, host.private_key)
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
    conn = libvirt_mgr.connect(host.ip_address, host.private_key)
    try:
        for vm in vms:
            dom = _domain_name(project_id, vm["id"])
            libvirt_mgr.destroy_vm(conn, dom)
    finally:
        conn.close()

    project.state = "stopped"
    db.commit()
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
    if project.state != "stopped":
        raise HTTPException(status_code=409, detail=f"Project is {project.state}, not stopped")

    project.state = "starting"
    db.commit()

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
        from app.services.deploy_service import check_host_disk_space
        disk = check_host_disk_space(host.ip_address, host.private_key)
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

    conn = libvirt_mgr.connect(host.ip_address, host.private_key)
    try:
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
                state = libvirt_mgr.get_vm_state(conn, dom_name)
                if state == "not_found":
                    states[node["id"]] = "not_found"
                elif state == "running":
                    states[node["id"]] = "running"
                elif state == "shut_off":
                    states[node["id"]] = "stopped"
                else:
                    states[node["id"]] = state
        return {"states": states, "progress": progress}
    finally:
        conn.close()


@router.post("/{project_id}/vms/{vm_id}/start")
def start_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    conn = libvirt_mgr.connect(host.ip_address, host.private_key)
    try:
        return {"action": "start", "success": libvirt_mgr.start_vm(conn, dom)}
    finally:
        conn.close()


@router.post("/{project_id}/vms/{vm_id}/stop")
def stop_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    conn = libvirt_mgr.connect(host.ip_address, host.private_key)
    try:
        return {"action": "stop", "success": libvirt_mgr.shutdown_vm(conn, dom)}
    finally:
        conn.close()


@router.get("/{project_id}/vms/{vm_id}/status")
def get_vm_status(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    conn = libvirt_mgr.connect(host.ip_address, host.private_key)
    try:
        state = libvirt_mgr.get_vm_state(conn, dom)
        return {"state": state}
    finally:
        conn.close()


@router.post("/{project_id}/vms/{vm_id}/forcestop")
def forcestop_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    conn = libvirt_mgr.connect(host.ip_address, host.private_key)
    try:
        return {"action": "forcestop", "success": libvirt_mgr.destroy_vm(conn, dom)}
    finally:
        conn.close()


@router.post("/{project_id}/vms/{vm_id}/restart")
def restart_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    conn = libvirt_mgr.connect(host.ip_address, host.private_key)
    try:
        return {"action": "restart", "success": libvirt_mgr.reboot_vm(conn, dom)}
    finally:
        conn.close()


@router.get("/{project_id}/vms/{vm_id}/console")
def get_vm_console(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    conn = libvirt_mgr.connect(host.ip_address, host.private_key)
    try:
        vnc_port = libvirt_mgr.get_vnc_port(conn, dom)
    finally:
        conn.close()

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

    # Allocate VNIs for new networks before going async
    current = project.topology or {}
    deployed = project.deployed_topology or {}
    vni_map = dict(project.vni_map or {})
    diff = diff_topologies(current, deployed) if deployed else {"added_vms": [], "removed_vms": [], "changed_vms": [], "added_networks": [], "removed_networks": [], "has_changes": False}
    if diff["added_networks"]:
        from app.services.vxlan import allocate_vni
        for net_node in diff["added_networks"]:
            if net_node.get("data", {}).get("subtype") == "network" and net_node["id"] not in vni_map:
                vni_map[net_node["id"]] = allocate_vni(db)
    project.vni_map = vni_map
    project.state = "reconfiguring"
    db.commit()

    restart_vm_ids = set((body or {}).get("restart_vm_ids", []))
    p_id = project.id
    h_id = host.id
    h_ip = host.ip_address
    h_key = host.private_key

    import threading
    def _do_reconfigure():
        from app.core.database import SessionLocal
        from app.services.deploy_service import _vm_domain_name, _vm_dir, _disk_path, _resolve_boot_devs, cache_library_images, generate_incremental_script, _deploy_progress
        from app.services.cloud_init import generate_seed_iso_script, generate_metadata_service_script

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

            _deploy_progress[p_id] = {"step": "networking", "detail": "configuring"}

            from app.services.vxlan import build_host_network_config
            all_hosts = s.query(Host).filter(Host.state == "active").all()
            peer_ips = [ho.ip_address for ho in all_hosts if ho.ip_address]
            net_config = build_host_network_config(current, vni_map, peer_ips)
            net_script = generate_setup_script(net_config, h_ip)
            net_result = run_ssh_script(h_ip, h_key, net_script, timeout=120)
            if not net_result["success"]:
                proj.state = "error"
                proj.deploy_error = f"Network setup failed:\n{net_result['output'][-2000:]}"
                s.commit()
                _deploy_progress.pop(p_id, None)
                return

            meta_script = generate_metadata_service_script(p_id, current, vni_map)
            if meta_script:
                run_ssh_script(h_ip, h_key, meta_script, timeout=30)

            vm_dir = _vm_dir(p_id)
            conn = libvirt_mgr.connect(h_ip, h_key)
            errors = []
            try:
                for node in diff["removed_vms"]:
                    dom = _vm_domain_name(p_id, node["id"])
                    libvirt_mgr.undefine_vm(conn, dom)
                    run_ssh_script(h_ip, h_key, f"rm -f {vm_dir}/{node['id'][:8]}-*", timeout=15)

                vms = _extract_vms(current)
                added_ids = {n["id"] for n in diff["added_vms"]}
                removed_ids = {n["id"] for n in diff["removed_vms"]}
                for vm in vms:
                    if vm["node_id"] in added_ids or vm["node_id"] in removed_ids:
                        continue
                    dom = _vm_domain_name(p_id, vm["node_id"])
                    vm_disks = _find_vm_disks(vm["node_id"], current)
                    boot_devs = _resolve_boot_devs(vm, vm_disks, current)
                    vm_networks = _find_vm_networks(vm["node_id"], current, vni_map)
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
                    disk_cmds = []
                    any_disk_changed = False
                    needs_library_download = False
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
                            disk_cmds.append(f"rm -f {path}")
                        if d.get("source") == "library" and d.get("library_item_id"):
                            needs_library_download = True
                            cache_path = f"/var/lib/troshka/images/{d['library_item_id']}.{d['format']}"
                            disk_cmds.append(f"test -f {path} || qemu-img create -f {d['format']} -b {cache_path} -F {d['format']} {path} {d['size_gb']}G")
                        else:
                            disk_cmds.append(f"test -f {path} || qemu-img create -f {d['format']} {path} {d['size_gb']}G")
                        if size_grew and not image_changed:
                            disk_cmds.append(f"qemu-img resize {path} {d['size_gb']}G")
                    if vm.get("cloud_init"):
                        from app.services.deploy_service import _seed_path
                        cdrom_list.append(_seed_path(p_id, vm["node_id"]))
                    if any_disk_changed:
                        if needs_library_download:
                            _deploy_progress[p_id] = {"step": "checking images", "detail": ""}
                            cache_library_images(current, h_ip, h_key, s)
                        run_ssh_script(h_ip, h_key, f"mkdir -p {vm_dir}\n" + "\n".join(disk_cmds), timeout=300)

                    current_cfg = libvirt_mgr.get_vm_config(conn, dom)
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
                    if not libvirt_mgr.reconfigure_vm(conn, dom, boot_devs=boot_devs, vcpus=vm["vcpus"], ram_mb=vm["ram_gb"] * 1024, nics=nics, disks=disk_list, cdroms=cdrom_list, restart=needs_restart):
                        errors.append(f"Failed to reconfigure {dom}")

                if diff["added_vms"]:
                    _deploy_progress[p_id] = {"step": "downloading", "detail": "0%"}
                    def _progress(downloaded, total):
                        pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
                        _deploy_progress[p_id] = {"step": "downloading", "detail": pct}
                    cache_library_images(current, h_ip, h_key, s, progress_callback=_progress)
                    seed_script = generate_seed_iso_script(p_id, current)
                    if seed_script:
                        run_ssh_script(h_ip, h_key, seed_script, timeout=30)
                    _deploy_progress[p_id] = {"step": "creating", "detail": "VMs"}
                    add_diff = {"added_vms": diff["added_vms"], "removed_vms": [], "changed_vms": [], "added_networks": [], "removed_networks": [], "has_changes": True}
                    script = generate_incremental_script(p_id, current, add_diff, vni_map)
                    result = run_ssh_script(h_ip, h_key, script, timeout=300)
                    if not result["success"]:
                        errors.append(f"Failed to add VMs: {result['output'][-300:]}")
            finally:
                conn.close()

            from app.services.placement import sync_host_capacity
            sync_host_capacity(s, h)

            proj.state = "active"
            if not errors:
                proj.deployed_topology = current
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
    host_ip = host.ip_address
    private_key = host.private_key
    target_vm_id = vm_id

    import threading
    def _do_redeploy():
        from app.core.database import SessionLocal
        from app.services.deploy_service import generate_incremental_script, run_ssh_script, _vm_domain_name, _vm_dir
        from app.services.cloud_init import generate_seed_iso_script

        s = SessionLocal()
        try:
            proj = s.query(Project).filter_by(id=p_id).first()
            h = s.query(Host).filter_by(id=host_id).first()
            if not proj or not h:
                return

            dom = _vm_domain_name(p_id, target_vm_id)
            vm_dir = _vm_dir(p_id)
            topology = proj.topology
            vni_map = proj.vni_map or {}

            conn = libvirt_mgr.connect(host_ip, private_key)
            was_running = False
            try:
                was_running = libvirt_mgr.get_vm_state(conn, dom) == "running"
                libvirt_mgr.undefine_vm(conn, dom, remove_storage=False)
            finally:
                conn.close()

            _redeploy_progress[dom] = {"step": "preparing", "detail": ""}
            run_ssh_script(host_ip, private_key, f"rm -f {vm_dir}/{target_vm_id[:8]}-*", timeout=15)

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
            from app.services.deploy_service import cache_library_images
            def _progress(downloaded, total):
                pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
                _redeploy_progress[dom] = {"step": "downloading", "detail": pct}
            cache_library_images(vm_topo, host_ip, private_key, s, progress_callback=_progress)

            seed_script = generate_seed_iso_script(p_id, topology)
            if seed_script:
                _redeploy_progress[dom] = {"step": "creating", "detail": "cloud-init seed ISO"}
                run_ssh_script(host_ip, private_key, seed_script, timeout=15)

            _redeploy_progress[dom] = {"step": "creating", "detail": "VM definition"}
            diff = {"added_vms": [vm_node], "removed_vms": [], "changed_vms": [], "added_networks": [], "removed_networks": [], "has_changes": True}
            script = generate_incremental_script(p_id, topology, diff, vni_map)
            run_ssh_script(host_ip, private_key, script, timeout=7200)

            if was_running:
                conn = libvirt_mgr.connect(host_ip, private_key)
                try:
                    libvirt_mgr.start_vm(conn, dom)
                finally:
                    conn.close()

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

    # Destroy existing
    if project.host_id:
        destroy_project_sync(project.id)

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

    # Clean up infrastructure if deployed
    if project.host_id and project.state in ("active", "stopped", "error"):
        destroy_project_sync(project.id)

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

    project.topology = topology
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "topology")
    db.commit()
    db.refresh(project)
    return project
