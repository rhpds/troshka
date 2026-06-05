import re
import shlex

from fastapi import APIRouter, Depends, HTTPException

VM_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$')
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.placement import place_project, calculate_project_requirements
from app.models.host import Host
from app.services.deploy_service import deploy_project_async, stop_project_async, start_project_async, destroy_project_sync, run_ssh_script
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

    result = place_project(db, project)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])

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


def _get_project_and_host(project_id: str, user: User, db: Session):
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
    return project, host


def _validate_vm_name(vm_name: str) -> str:
    if not VM_NAME_RE.match(vm_name):
        raise HTTPException(status_code=400, detail="Invalid VM name")
    return vm_name


@router.post("/{project_id}/vms/{vm_name}/start")
def start_vm(project_id: str, vm_name: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _validate_vm_name(vm_name)
    project, host = _get_project_and_host(project_id, user, db)
    full_name = f"troshka-{project_id[:8]}-{vm_name}"
    result = run_ssh_script(host.ip_address, host.private_key, f"virsh start {shlex.quote(full_name)}", timeout=30)
    return {"vm": full_name, "action": "start", "success": result["success"], "output": result["output"]}


@router.post("/{project_id}/vms/{vm_name}/stop")
def stop_vm(project_id: str, vm_name: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _validate_vm_name(vm_name)
    project, host = _get_project_and_host(project_id, user, db)
    full_name = f"troshka-{project_id[:8]}-{vm_name}"
    qname = shlex.quote(full_name)
    result = run_ssh_script(host.ip_address, host.private_key, f"virsh shutdown {qname} && echo SENT", timeout=15)
    return {"vm": full_name, "action": "stop", "success": result["success"], "output": result["output"]}


@router.get("/{project_id}/vms/{vm_name}/status")
def get_vm_status(project_id: str, vm_name: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _validate_vm_name(vm_name)
    project, host = _get_project_and_host(project_id, user, db)
    full_name = f"troshka-{project_id[:8]}-{vm_name}"
    result = run_ssh_script(host.ip_address, host.private_key, f"virsh domstate {shlex.quote(full_name)}", timeout=15)
    state = ""
    if result["success"]:
        for line in result["output"].strip().split("\n"):
            line = line.strip()
            if line in ("running", "shut off", "paused", "crashed", "dying"):
                state = line
                break
    return {"vm": full_name, "state": state}


@router.post("/{project_id}/vms/{vm_name}/forcestop")
def forcestop_vm(project_id: str, vm_name: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _validate_vm_name(vm_name)
    project, host = _get_project_and_host(project_id, user, db)
    full_name = f"troshka-{project_id[:8]}-{vm_name}"
    result = run_ssh_script(host.ip_address, host.private_key, f"virsh destroy {shlex.quote(full_name)}", timeout=30)
    return {"vm": full_name, "action": "forcestop", "success": result["success"], "output": result["output"]}


@router.post("/{project_id}/vms/{vm_name}/restart")
def restart_vm(project_id: str, vm_name: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _validate_vm_name(vm_name)
    project, host = _get_project_and_host(project_id, user, db)
    full_name = f"troshka-{project_id[:8]}-{vm_name}"
    qname = shlex.quote(full_name)
    result = run_ssh_script(host.ip_address, host.private_key, f"virsh reboot {qname} && echo SENT", timeout=15)
    return {"vm": full_name, "action": "restart", "success": result["success"], "output": result["output"]}


@router.get("/{project_id}/vms/{vm_name}/console")
def get_vm_console(project_id: str, vm_name: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _validate_vm_name(vm_name)
    project, host = _get_project_and_host(project_id, user, db)
    full_name = f"troshka-{project_id[:8]}-{vm_name}"
    result = run_ssh_script(host.ip_address, host.private_key, f"virsh vncdisplay {shlex.quote(full_name)}", timeout=15)
    vnc_display = ""
    if result["success"]:
        for line in result["output"].strip().split("\n"):
            line = line.strip()
            if line.startswith(":") or line.startswith("0.0.0.0:") or line.startswith("127.0.0.1:"):
                vnc_display = line
                break
    vnc_port = ""
    if ":" in vnc_display:
        display_num = vnc_display.split(":")[-1]
        vnc_port = str(5900 + int(display_num))
    if not vnc_port:
        return {"vm": full_name, "error": "VNC not available"}

    proxy = get_or_create_proxy(full_name, host.ip_address, host.private_key, int(vnc_port))
    if "error" in proxy:
        return {"vm": full_name, "error": proxy["error"]}

    return {
        "vm": full_name,
        "ws_port": proxy["ws_port"],
        "ws_url": proxy["ws_url"],
    }


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

    # Destroy existing
    if project.host_id:
        destroy_project_sync(project.id)

    # Reset for fresh deploy
    project.state = "draft"
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
