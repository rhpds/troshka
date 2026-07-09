import datetime
import logging
import uuid as uuid_mod
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.models.host import Host
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.deploy_service import (  # noqa: F401
    _create_seed_isos_via_troshkad,
    _create_vm_disks_via_troshkad,
    _create_vm_via_troshkad,
    _disk_path,
    _extract_vms,
    _find_vm_disks,
    _find_vm_networks,
    _seed_path,
    _setup_networks_via_troshkad,
    _setup_pxe_via_troshkad,
    _teardown_networks_via_troshkad,
    _vm_dir,
    cache_library_images,
    deploy_project_async,
    destroy_project_sync,
    diff_topologies,
    start_project_async,
    stop_project_async,
)
from app.services.placement import calculate_project_requirements, place_project
from app.services.troshkad_client import (
    TroshkadError,
    start_job,
    troshkad_download_from_vm,
    troshkad_upload_to_vm,
    wait_for_job,
)
from app.services.troshkad_client import (
    get_vm_config as troshkad_get_vm_config,
)
from app.services.troshkad_client import (
    get_vm_state as troshkad_get_vm_state,
)
from app.services.troshkad_client import (
    get_vnc_port as troshkad_get_vnc_port,
)
from app.services.troshkad_client import (
    reconfigure_vm as troshkad_reconfigure_vm,
)
from app.services.troshkad_client import (
    undefine_vm as troshkad_undefine_vm,
)
from app.services.ws_pubsub import notify_project

router = APIRouter(prefix="/projects", tags=["projects"])


def _project_response_dict(project):
    result = {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "owner_id": project.owner_id,
        "provider_id": project.provider_id,
        "host_type": project.host_type,
        "host_id": project.host_id,
        "guid": project.guid,
        "state": project.state,
        "public_token": project.public_token,
        "guest_permission": project.guest_permission,
        "topology": project.topology,
        "deployed_topology": project.deployed_topology,
        "vni_map": project.vni_map,
        "deploy_error": project.deploy_error,
        "ocp_status": project.ocp_status,
        "ocp_install_elapsed": project.ocp_install_elapsed,
        "tags": project.tags,
        "auto_stop_minutes": project.auto_stop_minutes,
        "auto_stop_expires_at": (
            project.auto_stop_expires_at.isoformat()
            if project.auto_stop_expires_at
            else None
        ),
        "auto_delete_minutes": project.auto_delete_minutes,
        "auto_stopped": project.auto_stopped,
        "lifetime_expires_at": (
            project.lifetime_expires_at.isoformat()
            if project.lifetime_expires_at
            else None
        ),
        "poweroff_mode": project.poweroff_mode,
        "clock_target": (
            project.clock_target.isoformat() if project.clock_target else None
        ),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }
    deployed_topo = project.deployed_topology or {}
    bmc_data = deployed_topo.get("bmc")
    if bmc_data:
        result["bmc"] = bmc_data
    if project.host_id:
        from app.models.host import Host
        from app.models.provider import Provider

        from sqlalchemy.orm import Session as _S

        s: _S = object.__getattribute__(project, "_sa_instance_state").session
        if s:
            h = s.get(Host, project.host_id)
            if h and h.provider_id:
                prov = s.get(Provider, h.provider_id)
                if prov:
                    result["provider_type"] = prov.type
    return result


@router.get("/", response_model=list[ProjectResponse])
def list_projects(
    skip: int = 0,
    limit: int = 50,
    guid: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Project).filter(Project.owner_id == user.id)
    if guid is not None:
        query = query.filter(Project.guid == guid)
    projects = query.offset(skip).limit(limit).all()

    host_ids = {p.host_id for p in projects if p.host_id}
    hosts_by_id = {}
    provs_by_id = {}
    if host_ids:
        from app.models.host import Host
        from app.models.provider import Provider

        hosts = db.query(Host).filter(Host.id.in_(host_ids)).all()
        hosts_by_id = {h.id: h for h in hosts}
        prov_ids = {h.provider_id for h in hosts if h.provider_id}
        if prov_ids:
            provs_by_id = {
                pv.id: pv
                for pv in db.query(Provider).filter(Provider.id.in_(prov_ids)).all()
            }

    results = []
    for p in projects:
        resp = ProjectResponse.model_validate(p)
        h = hosts_by_id.get(p.host_id) if p.host_id else None
        if h:
            resp.host_instance_id = h.instance_id
            resp.host_ip = h.ip_address
            prov = provs_by_id.get(h.provider_id) if h.provider_id else None
            if prov:
                resp.host_provider_name = prov.name
                resp.host_provider_type = prov.type
        results.append(resp)
    return results


@router.post("/", response_model=ProjectResponse, status_code=201)
def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(Project).filter_by(owner_id=user.id, name=body.name).first()
    if existing:
        raise HTTPException(
            status_code=409, detail=f'You already have a project named "{body.name}"'
        )

    project = Project(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        provider_id=body.provider_id,
        host_type=body.host_type,
        auto_stop_minutes=body.auto_stop_minutes,
        auto_delete_minutes=body.auto_delete_minutes,
        poweroff_mode=body.poweroff_mode,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/templates")
def list_topology_templates(user: User = Depends(get_current_user)):
    from app.services.template_loader import list_yaml_templates

    return list_yaml_templates()


@router.post("/auto-layout")
def auto_layout_topology(body: dict, user: User = Depends(get_current_user)):
    from app.services.auto_layout import auto_layout

    nodes = body.get("nodes", [])
    edges = body.get("edges", [])
    new_nodes, new_edges = auto_layout(nodes, edges)
    return {"nodes": new_nodes, "edges": new_edges}


def _build_pull_through_config(registry_url: str) -> dict:
    return {
        "enabled": True,
        "url": registry_url,
        "orgs": {
            "registry.redhat.io": "registry_redhat_io",
            "quay.io": "quay_io",
        },
    }


@router.post("/from-template", status_code=201)
def create_project_from_template(
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
        resolve_template,
    )

    template_yaml = body.get("template_yaml")
    template_id = body.get("template_id")

    if template_yaml:
        resolved = resolve_inline_template(template_yaml)
        template_id = resolved.get("name", "inline")
    elif template_id:
        try:
            resolved = resolve_template(template_id)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"Template '{template_id}' not found"
            )
    else:
        raise HTTPException(
            status_code=400, detail="template_id or template_yaml is required"
        )

    # Apply defaults from template's ocp section
    ocp_cfg = resolved.get("ocp", {})
    if ocp_cfg.get("cluster_name"):
        body.setdefault("cluster_name", ocp_cfg["cluster_name"])
    if ocp_cfg.get("base_domain"):
        body.setdefault("base_domain", ocp_cfg["base_domain"])

    common_password = body.get("common_password", "")
    external_access = body.get("external_access", False)
    block_outbound = body.get("block_outbound", True)

    if not block_outbound:
        resolved.setdefault("gateway", {}).pop("outbound_ports", None)

    topology = generate_topology_from_template(
        resolved,
        bmc_password=common_password,
        external_access=external_access,
    )

    # OCP template customization — resolve DB objects, then delegate to plugin
    from app.models.library import Library, LibraryItem

    bastion_image_id = body.get("bastion_image_id")
    bastion_image = None
    if bastion_image_id:
        item = db.query(LibraryItem).filter_by(id=bastion_image_id).first()
    else:
        item = (
            db.query(LibraryItem)
            .join(Library)
            .filter(
                Library.owner_id == user.id,
                LibraryItem.tags["ocp_default_image"].as_boolean(),
            )
            .first()
        )
    if item:
        bastion_image = {
            "id": item.id,
            "name": item.name,
            "size_gb": max(1, (item.size_bytes or 0) // (1024**3)),
        }

    bastion_iso_id = body.get("bastion_iso_id")
    bastion_iso = None
    if bastion_iso_id:
        iso_item = db.query(LibraryItem).filter_by(id=bastion_iso_id).first()
    else:
        iso_item = (
            db.query(LibraryItem)
            .join(Library)
            .filter(
                Library.owner_id == user.id,
                LibraryItem.tags["ocp_default_iso"].as_boolean(),
            )
            .first()
        )
    if iso_item:
        bastion_iso = {
            "id": iso_item.id,
            "name": iso_item.name,
            "size_bytes": iso_item.size_bytes or 0,
        }

    ssh_pub_key = body.get("ssh_pub_key", "")
    ssh_key_ids = []
    ssh_keys = [ssh_pub_key] if ssh_pub_key else []
    bastion_ssh_key_id = body.get("bastion_ssh_key_id")
    if bastion_ssh_key_id:
        from app.models.user import UserSshKey

        ssh_key = (
            db.query(UserSshKey)
            .filter_by(id=bastion_ssh_key_id, user_id=user.id)
            .first()
        )
        if ssh_key:
            ssh_pub_key = ssh_key.public_key
            ssh_key_ids = [ssh_key.id]
            ssh_keys = [ssh_key.public_key]

    pull_secret_json = ""
    if user.ocp_pull_secret:
        from app.core.encryption import decrypt

        pull_secret_json = decrypt(user.ocp_pull_secret)

    if not resolved.get("pull_through_registry") and user.pull_through_registry:
        if user.pull_through_registry_url:
            resolved["pull_through_registry"] = _build_pull_through_config(
                user.pull_through_registry_url
            )

    import ipaddress as _ipaddr

    bmc_ip_raw = body.get("bastion_bmc_ip", "192.168.100.50")
    try:
        str(_ipaddr.IPv4Address(bmc_ip_raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid bastion BMC IP")

    from app.services.ocp.agent_template import customize_topology as customize_ocp

    if resolved.get("category") != "openshift":
        # Non-OCP template — attach bastion image, set password and SSH keys
        from app.services.ocp.agent_template import (
            _attach_bastion_image,
            _attach_bastion_iso,
        )

        _attach_bastion_image(topology, bastion_image)
        _attach_bastion_iso(topology, bastion_iso)
        for node in topology.get("nodes", []):
            if (
                node.get("type") == "vmNode"
                and node.get("data", {}).get("name") == "bastion"
            ):
                node["data"]["cloudInit"] = True
                if common_password:
                    node["data"]["ciCloudUserPassword"] = common_password
                if ssh_key_ids:
                    node["data"]["ciSshKeyIds"] = ssh_key_ids
                if ssh_keys:
                    node["data"]["ciSshKeys"] = ssh_keys
                break
    else:
        customize_ocp(
            topology,
            template_id,
            {
                "cluster_name": body.get("cluster_name", "ocp"),
                "base_domain": body.get("base_domain", "ocp.local"),
                "ocp_version": body.get("ocp_version", "4.20"),
                "common_password": common_password,
                "pull_secret_json": pull_secret_json,
                "ssh_pub_key": ssh_pub_key,
                "ssh_key_ids": ssh_key_ids,
                "ssh_keys": ssh_keys,
                "bastion_image": bastion_image,
                "bastion_iso": bastion_iso,
                "bastion_bmc_ip": bmc_ip_raw,
                "auto_install_ocp": body.get("auto_install_ocp", True),
                "resolved": resolved,
            },
        )

    desc_parts = [resolved.get("description", "")]
    cluster_name = body.get("cluster_name", "ocp")
    base_domain = body.get("base_domain", "ocp.local")
    ocp_version = body.get("ocp_version", "")
    if ocp_version:
        desc_parts.append(f"OCP {ocp_version}")
    desc_parts.append(f"API: api.{cluster_name}.{base_domain}")

    from app.services.deploy_service import (
        validate_topology_ips,
        validate_topology_names,
    )

    topo_errors = validate_topology_names(topology) + validate_topology_ips(topology)
    if topo_errors:
        raise HTTPException(
            status_code=400,
            detail="Template produces duplicate names: " + "; ".join(topo_errors),
        )

    project = Project(
        name=body.get("name", resolved.get("display_name", template_id)),
        description=" | ".join(desc_parts),
        owner_id=user.id,
        topology=topology,
    )

    clock_target_str = body.get("clock_target") or resolved.get("clock_target")
    if clock_target_str:
        from datetime import datetime

        if isinstance(clock_target_str, str):
            ct = datetime.fromisoformat(clock_target_str.replace("Z", "+00:00"))
        else:
            ct = clock_target_str
        project.clock_target = ct

    db.add(project)
    db.commit()
    db.refresh(project)
    return {"id": project.id, "name": project.name}


@router.post("/{project_id}/import-template")
def import_template(
    project_id: str,
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state != "draft":
        raise HTTPException(
            status_code=409, detail="Can only import template on draft projects"
        )

    template_yaml = body.get("template_yaml")
    if not template_yaml:
        raise HTTPException(status_code=400, detail="template_yaml is required")
    if not isinstance(template_yaml, dict):
        raise HTTPException(
            status_code=400, detail="template_yaml must be a YAML mapping"
        )
    if "vms" not in template_yaml:
        raise HTTPException(
            status_code=400, detail="Template must contain a 'vms' section"
        )
    if "networks" not in template_yaml:
        raise HTTPException(
            status_code=400, detail="Template must contain a 'networks' section"
        )

    # Validate library item references exist and belong to this user
    from app.models.library import Library, LibraryItem

    missing = []
    vms_def = template_yaml.get("vms", {})

    def _resolve_library_item(item_id, item_name, label):
        """Look up a library item by ID, falling back to name."""
        if item_id:
            item = (
                db.query(LibraryItem)
                .join(Library)
                .filter(LibraryItem.id == item_id, Library.owner_id == user.id)
                .first()
            )
            if item:
                return item
        if item_name:
            item = (
                db.query(LibraryItem)
                .join(Library)
                .filter(LibraryItem.name == item_name, Library.owner_id == user.id)
                .first()
            )
            if item:
                return item
        if item_id or item_name:
            missing.append(f"{label}: '{item_name or item_id}' not found")
        return None

    for vm_name, vm_cfg in vms_def.items():
        for di, disk_cfg in enumerate(vm_cfg.get("disks", [])):
            item = _resolve_library_item(
                disk_cfg.get("library_item_id"),
                disk_cfg.get("library_item_name"),
                f"VM '{vm_name}' disk {di}",
            )
            if item:
                disk_cfg["library_item_id"] = item.id
                disk_cfg["library_item_name"] = item.name
        iso_id = vm_cfg.get("pxe_boot_iso_id")
        if iso_id:
            item = _resolve_library_item(
                iso_id,
                vm_cfg.get("pxe_boot_iso_name"),
                f"VM '{vm_name}' PXE boot ISO",
            )
            if item:
                vm_cfg["pxe_boot_iso_id"] = item.id
                vm_cfg["pxe_boot_iso_name"] = item.name
        for ii, iso_cfg in enumerate(vm_cfg.get("isos", [])):
            item = _resolve_library_item(
                iso_cfg.get("library_item_id"),
                iso_cfg.get("library_item_name"),
                f"VM '{vm_name}' ISO {ii}",
            )
            if item:
                iso_cfg["library_item_id"] = item.id
                iso_cfg["library_item_name"] = item.name
    if missing:
        raise HTTPException(
            status_code=400,
            detail="Library items not found:\n" + "\n".join(missing),
        )

    try:
        resolved = resolve_inline_template(template_yaml)
        topology = generate_topology_from_template(resolved)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid template: {e}")

    from app.services.deploy_service import (
        validate_topology_ips,
        validate_topology_names,
    )

    topo_errors = validate_topology_names(topology) + validate_topology_ips(topology)
    if topo_errors:
        raise HTTPException(
            status_code=400,
            detail="Template produces duplicate names: " + "; ".join(topo_errors),
        )

    project.topology = topology

    clock_target_str = resolved.get("clock_target")
    if clock_target_str:
        from datetime import datetime

        if isinstance(clock_target_str, str):
            ct = datetime.fromisoformat(clock_target_str.replace("Z", "+00:00"))
        else:
            ct = clock_target_str
        project.clock_target = ct

    db.add(project)
    db.commit()
    db.refresh(project)

    return {"topology": topology}


_PASSWORD_FIELDS = {
    "vm": ["cloud_user_password"],
    "network": ["bmc_password"],
}


def _apply_password_mode(result: dict, mode: str, custom: str = ""):
    if mode == "current":
        return
    for net_cfg in result.get("networks", {}).values():
        if mode == "none":
            net_cfg.pop("bmc_password", None)
        elif mode == "custom" and custom:
            if "bmc_password" in net_cfg:
                net_cfg["bmc_password"] = custom
    for vm_cfg in result.get("vms", {}).values():
        if mode == "none":
            vm_cfg.pop("cloud_user_password", None)
        elif mode == "custom" and custom:
            if "cloud_user_password" in vm_cfg:
                vm_cfg["cloud_user_password"] = custom


@router.post("/{project_id}/export-template")
def export_template(
    project_id: str,
    body: dict | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.template_loader import export_topology_to_template

    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    topo = project.topology or {}
    result = export_topology_to_template(topo, db=db)
    result["name"] = project.name
    if project.description:
        result["description"] = project.description
    if project.clock_target:
        result["clock_target"] = project.clock_target.isoformat()

    ocp_meta = topo.get("ocpMeta", {})
    if ocp_meta.get("clusterName"):
        result["ocp"] = {
            "cluster_name": ocp_meta["clusterName"],
            "base_domain": ocp_meta.get("baseDomain", "ocp.local"),
        }

    for key in ("disconnected", "bastion_services", "dns_records"):
        if topo.get(key):
            result[key] = topo[key]

    # Apply password mode
    body = body or {}
    pw_mode = body.get("password_mode", "current")
    pw_custom = body.get("custom_password", "")  # pragma: allowlist secret
    _apply_password_mode(result, pw_mode, pw_custom)

    if not body.get("include_ids"):
        for vm_cfg in result.get("vms", {}).values():
            for disk in vm_cfg.get("disks", []):
                disk.pop("library_item_id", None)
            for iso in vm_cfg.get("isos", []):
                iso.pop("library_item_id", None)
            vm_cfg.pop("pxe_boot_iso_id", None)

    import yaml  # type: ignore[import-untyped]
    from fastapi.responses import Response

    yaml_str = yaml.dump(result, default_flow_style=False, sort_keys=False)
    if pw_mode == "none":
        header = "# Troshka infra_template export\n# Passwords omitted — set them before deploying.\n\n"
    else:
        header = "# Troshka infra_template export\n# WARNING: Passwords are stored in plain text.\n\n"
    return Response(content=header + yaml_str, media_type="text/yaml")


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

    return _project_response_dict(project)


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
    from app.services.deploy_service import get_deploy_progress as _get_dp

    progress = _get_dp(project_id)
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

    fields = body.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(project, field, value)

    # Auto-stop timer recomputation
    if "auto_stop_minutes" in fields:
        if fields["auto_stop_minutes"] is None:
            project.auto_stop_started_at = None
            project.auto_stop_expires_at = None
            project.auto_stop_warned = False
        else:
            now = datetime.datetime.now(datetime.UTC)
            if not project.auto_stop_started_at and project.state == "active":
                project.auto_stop_started_at = now
            if project.auto_stop_started_at:
                project.auto_stop_expires_at = (
                    project.auto_stop_started_at
                    + datetime.timedelta(minutes=project.auto_stop_minutes)
                )
            project.auto_stop_warned = False

    # Auto-delete timer recomputation
    if "auto_delete_minutes" in fields:
        if fields["auto_delete_minutes"] is None:
            project.auto_delete_started_at = None
            project.lifetime_expires_at = None
            project.auto_delete_warned = False
        else:
            now = datetime.datetime.now(datetime.UTC)
            if not project.auto_delete_started_at and project.state != "draft":
                project.auto_delete_started_at = now
            if project.auto_delete_started_at:
                project.lifetime_expires_at = (
                    project.auto_delete_started_at
                    + datetime.timedelta(minutes=project.auto_delete_minutes)
                )
            project.auto_delete_warned = False

    # Live clock adjustment
    if "clock_target" in fields and project.state == "active":
        from app.services.clock_service import adjust_clocks_async

        adjust_clocks_async(project_id)

    db.commit()
    db.refresh(project)
    if "topology" in fields:
        notify_project(
            project_id, {"type": "topology-update", "topology": project.topology}
        )
    return _project_response_dict(project)


class ExtendTimerRequest(PydanticBaseModel):
    timer: str  # "auto_stop" or "auto_delete"
    add_minutes: int


@router.post("/{project_id}/extend-timer")
def extend_timer(
    project_id: str,
    body: ExtendTimerRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    if body.timer == "auto_stop":
        if not project.auto_stop_expires_at:
            raise HTTPException(status_code=400, detail="Auto-stop timer is not active")
        project.auto_stop_expires_at += datetime.timedelta(minutes=body.add_minutes)
        project.auto_stop_warned = False
    elif body.timer == "auto_delete":
        if not project.lifetime_expires_at:
            raise HTTPException(
                status_code=400, detail="Auto-delete timer is not active"
            )
        project.lifetime_expires_at += datetime.timedelta(minutes=body.add_minutes)
        project.auto_delete_warned = False
    else:
        raise HTTPException(
            status_code=400, detail="timer must be 'auto_stop' or 'auto_delete'"
        )

    db.commit()
    db.refresh(project)
    return _project_response_dict(project)


@router.post("/{project_id}/deploy")
def deploy_project(
    project_id: str,
    storage_pool_id: str | None = None,
    host_id: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state != "draft":
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, not draft"
        )
    if not project.topology:
        raise HTTPException(status_code=400, detail="Project has no topology")

    from app.services.deploy_service import (
        validate_topology_ips,
        validate_topology_names,
    )

    topo_errors = validate_topology_names(project.topology) + validate_topology_ips(
        project.topology
    )
    if topo_errors:
        raise HTTPException(
            status_code=400,
            detail="Topology has errors: " + "; ".join(topo_errors),
        )

    reqs = calculate_project_requirements(project.topology)
    if reqs["vm_count"] == 0:
        raise HTTPException(status_code=400, detail="Project has no VMs")

    # Validate BMC network has at least one connected provisioner VM
    topology = project.topology or {}
    bmc_network = None
    for node in topology.get("nodes", []):
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("networkType") == "bmc"
        ):
            bmc_network = node
            break
    if bmc_network:
        bmc_edges = [
            e
            for e in topology.get("edges", [])
            if e.get("source") == bmc_network["id"]
            or e.get("target") == bmc_network["id"]
        ]
        if not bmc_edges:
            raise HTTPException(
                status_code=400,
                detail="BMC network requires at least one connected VM to act as a provisioner",
            )

    _check_library_items_ready(project.topology, db)

    # Pool/host selection: admin can specify, otherwise auto-select
    if (storage_pool_id or host_id) and user.role != "admin":
        raise HTTPException(
            status_code=403, detail="Only admins can select a storage pool or host"
        )
    if storage_pool_id:
        from app.models.storage_pool import StoragePool

        pool = db.query(StoragePool).get(storage_pool_id)
        if not pool:
            raise HTTPException(status_code=404, detail="Storage pool not found")
        if pool.mode.startswith("shared") and pool.status != "available":
            raise HTTPException(
                status_code=400, detail=f"Pool is not available (status: {pool.status})"
            )
    if host_id:
        from app.models.host import Host as _Host

        target_host = db.query(_Host).filter_by(id=host_id).first()
        if not target_host:
            raise HTTPException(status_code=404, detail="Host not found")
        if target_host.state != "active" or target_host.agent_status != "connected":
            raise HTTPException(
                status_code=400,
                detail=f"Host is not available (state={target_host.state}, agent={target_host.agent_status})",
            )

    result = place_project(
        db, project, storage_pool_id=storage_pool_id, host_id=host_id
    )
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])

    from app.services.troshkad_client import check_disk_usage

    host = db.query(Host).filter_by(id=result["host_id"]).first()
    if host and host.ip_address:
        try:
            disk = check_disk_usage(host)
            if disk:
                logger.info(
                    "Deploy %s: disk check — %s%% used, %.1f GB free",
                    project.id[:8],
                    disk["used_pct"],
                    disk["free_bytes"] / (1024**3),
                )
                if disk["used_pct"] >= 90:
                    free_gb = disk["free_bytes"] / (1024**3)
                    project.state = "draft"
                    db.commit()
                    raise HTTPException(
                        status_code=507,
                        detail=f"Host storage is {disk['used_pct']}% full ({free_gb:.1f} GB free). Free space or resize the volume before deploying.",
                    )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(
                "Deploy %s: disk check failed (non-fatal): %s", project.id[:8], e
            )

    # Persist VNI map for stop/start/destroy
    project.vni_map = result.get("vni_map")
    db.commit()

    # Deploy in background
    import threading

    threading.Thread(
        target=deploy_project_async,
        args=(project.id,),
        daemon=True,
        name=f"deploy-{project.id[:8]}",
    ).start()

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
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, not active"
        )

    project.state = "stopping"
    db.commit()
    notify_project(
        project_id, {"type": "project-state", "state": "stopping", "deploy_error": None}
    )

    import threading

    threading.Thread(
        target=stop_project_async,
        args=(project.id,),
        daemon=True,
        name=f"stop-{project.id[:8]}",
    ).start()

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
            job_id = start_job(host, "/vms/force-off", {"domain_name": dom})
            wait_for_job(host, job_id, timeout=30, poll_interval=2)
        except TroshkadError:
            logger.warning("Failed to force-stop VM %s", dom)

    project.state = "stopped"
    db.commit()
    notify_project(
        project_id, {"type": "project-state", "state": "stopped", "deploy_error": None}
    )
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
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, not stopped"
        )

    project.state = "starting"
    db.commit()
    notify_project(
        project_id, {"type": "project-state", "state": "starting", "deploy_error": None}
    )

    import threading

    threading.Thread(
        target=start_project_async,
        args=(project.id,),
        daemon=True,
        name=f"start-{project.id[:8]}",
    ).start()

    return {"status": "starting"}


def _get_project_and_host(
    project_id: str, user: User, db: Session, check_disk: bool = False
):
    """Helper to load project + host with auth and state checks."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if project.state not in ("active", "stopped"):
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, VMs not accessible"
        )
    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.private_key or not host.ip_address:
        raise HTTPException(status_code=503, detail="Host not available")
    if check_disk:
        from app.services.troshkad_client import check_disk_usage

        disk = check_disk_usage(host)
        if disk and disk["used_pct"] >= 90:
            free_gb = disk["free_bytes"] / (1024**3)
            raise HTTPException(
                status_code=507,
                detail=f"Host storage is {disk['used_pct']}% full ({free_gb:.1f} GB free). Free space or resize the volume.",
            )
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
                    raise HTTPException(
                        status_code=400,
                        detail=f"Library item not found for '{node['data'].get('name', 'storage')}'",
                    )
                if lib_item.state != "ready":
                    raise HTTPException(
                        status_code=400,
                        detail=f"'{lib_item.name}' is still {lib_item.state}. Wait for it to finish.",
                    )


def _domain_name(project_id: str, vm_id: str) -> str:
    from app.services.deploy_service import _vm_domain_name

    return _vm_domain_name(project_id, vm_id)


@router.get("/{project_id}/vm-states")
def get_all_vm_states(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get actual running state of all VMs from libvirt."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    if not project.host_id:
        return {"states": {}}

    # Return cached states from the background poller when available
    # (avoids blocking troshkad calls on every browser poll)
    from app.services.ws_pubsub import get_cached_vm_states

    cached = get_cached_vm_states(project_id)
    if cached:
        return cached

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.private_key or not host.ip_address:
        return {"states": {}}

    states: dict[str, Any] = {}
    container_states: dict[str, Any] = {}
    progress: dict[str, Any] = {}

    if host.agent_status != "connected":
        for node in (project.topology or {}).get("nodes", []):
            if node.get("type") == "vmNode":
                states[node["id"]] = "unknown"
            elif node.get("type") == "containerNode":
                container_states[node["id"]] = "unknown"
        return {
            "states": states,
            "container_states": container_states,
            "progress": progress,
        }

    from app.services.troshkad_client import get_all_container_states
    from app.services.troshkad_client import get_all_vm_states as troshkad_batch_states

    batch = troshkad_batch_states(host) or {}
    container_batch = get_all_container_states(host) or {}

    for node in (project.topology or {}).get("nodes", []):
        if node.get("type") == "vmNode":
            dom_name = _domain_name(project_id, node["id"])
            if dom_name in _redeploy_progress:
                states[node["id"]] = "redeploying"
                progress[node["id"]] = _redeploy_progress[dom_name]
            else:
                raw = batch.get(dom_name, "unknown")
                if raw == "not_found" or raw == "unknown":
                    states[node["id"]] = raw
                elif raw == "running":
                    states[node["id"]] = "running"
                elif raw == "shut_off":
                    states[node["id"]] = "stopped"
                else:
                    states[node["id"]] = raw
        elif node.get("type") == "containerNode":
            ctr_name = f"troshka-{project_id[:8]}-{node['id'][:8]}"
            ctr_info = container_batch.get(ctr_name, {})
            raw = (
                ctr_info.get("state", "unknown")
                if isinstance(ctr_info, dict)
                else ctr_info
            )
            state = "stopped" if raw == "exited" else raw
            container_states[node["id"]] = {
                "state": state,
                "ips": ctr_info.get("ips", []) if isinstance(ctr_info, dict) else [],
            }
    return {
        "states": states,
        "container_states": container_states,
        "progress": progress,
    }


@router.post("/{project_id}/vms/{vm_id}/start")
def start_vm(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)

    if project.state in ("stopped", "starting"):
        import threading

        project.state = "starting"
        db.commit()
        p_id = project.id
        h_id = host.id
        target_vm_id = vm_id

        def _start_infra_then_vm():
            import json

            from sqlalchemy import text

            from app.core.database import SessionLocal
            from app.models.elastic_ip import ElasticIp
            from app.models.host import Host as HostModel
            from app.models.project import Project
            from app.services.deploy_service import (
                _setup_networks_via_troshkad,
                cache_library_images,
            )
            from app.services.eip_service import associate_eip

            s = SessionLocal()
            try:
                proj = s.query(Project).filter_by(id=p_id).first()
                h = s.query(HostModel).filter_by(id=h_id).first()
                if not proj or not h:
                    return

                topology = proj.topology or {}
                vni_map = proj.vni_map or {}

                # Re-associate EIPs
                project_eips = (
                    s.query(ElasticIp)
                    .filter_by(project_id=p_id, state="allocated")
                    .all()
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
                        {"topo": json.dumps(topology), "pid": p_id},
                    )
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
                    notify_project(
                        p_id,
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
                    p_id,
                    {"type": "project-state", "state": "active", "deploy_error": None},
                )
                logger.info(
                    "Infra + VM %s started for project %s", target_vm_id[:8], p_id[:8]
                )
            except Exception:
                logger.exception("Failed to start infra for project %s", p_id[:8])
                proj = s.query(Project).filter_by(id=p_id).first()
                if proj:
                    proj.state = "error"
                    s.commit()
            finally:
                s.close()

        notify_project(
            project_id,
            {"type": "vm-state", "states": {vm_id: "starting"}, "progress": {}},
        )
        threading.Thread(
            target=_start_infra_then_vm, daemon=True, name=f"start-vm-{vm_id[:8]}"
        ).start()
        return {"action": "start", "success": True, "starting_project": True}

    # Start VM in background — re-cache images if needed, then virsh start
    notify_project(
        project_id, {"type": "vm-state", "states": {vm_id: "starting"}, "progress": {}}
    )
    import threading

    p_id = project.id
    h_id = host.id

    def _cache_and_start():
        from app.core.database import SessionLocal
        from app.services.deploy_service import cache_library_images

        s = SessionLocal()
        try:
            from app.models.host import Host as HostModel
            from app.models.project import Project

            proj = s.query(Project).filter_by(id=p_id).first()
            h = s.query(HostModel).filter_by(id=h_id).first()
            if proj and h:
                topo = proj.deployed_topology or proj.topology or {}
                cache_library_images(topo, h, s)
            dom = _domain_name(p_id, vm_id)
            try:
                job_id = start_job(h, "/vms/start", {"domain_name": dom})
                wait_for_job(h, job_id, timeout=60, poll_interval=2)
                notify_project(
                    p_id,
                    {"type": "vm-state", "states": {vm_id: "running"}, "progress": {}},
                )
            except TroshkadError as e:
                logger.error("Failed to start VM %s: %s", dom, e)
        finally:
            s.close()

    threading.Thread(
        target=_cache_and_start, daemon=True, name=f"cache-start-{project.id[:8]}"
    ).start()
    return {"action": "start", "success": True}


@router.post("/{project_id}/vms/{vm_id}/stop")
def stop_vm(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/stop", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=60, poll_interval=2)
        notify_project(
            project_id,
            {"type": "vm-state", "states": {vm_id: "stopped"}, "progress": {}},
        )
        return {"action": "stop", "success": True}
    except TroshkadError as e:
        logger.error("Failed to stop VM %s: %s", dom, e)
        return {"action": "stop", "success": False}


@router.get("/{project_id}/vms/{vm_id}/status")
def get_vm_status(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    vm_info = troshkad_get_vm_state(host, dom)
    return {"state": vm_info["state"], "boot_devs": vm_info.get("boot_devs", [])}


@router.post("/{project_id}/vms/{vm_id}/forcestop")
def forcestop_vm(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/force-off", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=30, poll_interval=2)
        notify_project(
            project_id,
            {"type": "vm-state", "states": {vm_id: "stopped"}, "progress": {}},
        )
        return {"action": "forcestop", "success": True}
    except TroshkadError as e:
        logger.error("Failed to force-stop VM %s: %s", dom, e)
        return {"action": "forcestop", "success": False}


@router.post("/{project_id}/vms/{vm_id}/restart")
def restart_vm(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/reboot", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=60, poll_interval=2)
        notify_project(
            project_id,
            {"type": "vm-state", "states": {vm_id: "running"}, "progress": {}},
        )
        return {"action": "restart", "success": True}
    except TroshkadError as e:
        logger.error("Failed to restart VM %s: %s", dom, e)
        return {"action": "restart", "success": False}


@router.get("/{project_id}/vms/{vm_id}/console")
def get_vm_console(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    vnc_port = troshkad_get_vnc_port(host, dom)

    if not vnc_port:
        return {"error": "VNC not available"}

    if not host.console_domain or not host.agent_token:
        return {"error": "Console proxy not configured for this host"}

    from app.services.console_dns import sign_console_jwt

    jwt = sign_console_jwt(dom, host.id, host.agent_token)
    return {"ws_url": f"wss://{host.console_domain}/ws/{jwt}"}


@router.get("/{project_id}/vms/{vm_id}/ready")
def vm_ready(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check if a VM is SSH-reachable via the exec API."""
    project, host = _get_project_and_host(project_id, user, db)
    if project.state not in ("active", "stopped", "deploying"):
        return {"ready": False, "reason": f"project is {project.state}"}
    if not host:
        return {"ready": False, "reason": "no host assigned"}

    vm_node = next(
        (n for n in (project.topology or {}).get("nodes", []) if n["id"] == vm_id),
        None,
    )
    if not vm_node:
        raise HTTPException(status_code=404, detail="VM not found")

    vm_ip = ""
    for nic in vm_node.get("data", {}).get("nics", []):
        if nic.get("ip"):
            vm_ip = nic["ip"]
            break

    password = vm_node.get("data", {}).get("ciCloudUserPassword", "")
    if not vm_ip:
        return {"ready": False, "reason": "no IP"}
    if not password:
        return {"ready": False, "reason": "no password"}

    try:
        job_id = start_job(
            host,
            "/vm/ssh-exec",
            {
                "project_id": project_id,
                "vm_ip": vm_ip,
                "username": "cloud-user",
                "password": password,
                "command": "echo ok",
                "timeout": 5,
            },
        )
        job = wait_for_job(host, job_id, timeout=15)
        if job["status"] == "completed":
            output = job.get("result", {}).get("output", "")
            return {"ready": "ok" in output, "vm_id": vm_id}
    except TroshkadError as e:
        return {"ready": False, "reason": str(e)}

    return {"ready": False, "reason": "exec failed"}


@router.post("/{project_id}/vms/{vm_id}/exec")
def vm_exec(
    project_id: str,
    vm_id: str,
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Execute a command on a VM.

    Body params:
        command: Shell command to execute (required)
        username: SSH/console user (default: cloud-user)
        password: VM password (auto-resolved from topology if omitted)
        timeout: Command timeout in seconds (default: 600, max: 3600)
        method: "auto" (tries ssh → serial → console), "ssh", "serial", or "console"
    """
    project, host = _get_project_and_host(project_id, user, db)
    if project.state not in ("active", "stopped"):
        raise HTTPException(status_code=409, detail="Project must be active")

    command = body.get("command", "")
    if not command:
        raise HTTPException(status_code=400, detail="Command is required")

    vm_node = next(
        (n for n in (project.topology or {}).get("nodes", []) if n["id"] == vm_id),
        None,
    )
    username = body.get("username", "cloud-user")
    password = body.get("password", "")
    if not password and vm_node:
        password = vm_node.get("data", {}).get("ciCloudUserPassword", "")

    timeout = min(body.get("timeout", 600), 3600)
    method = body.get("method", "auto")
    if body.get("use_ssh"):
        method = "ssh"
    force_tty = method == "console-text"
    if force_tty:
        method = "console"

    vm_ip = ""
    if vm_node:
        for nic in vm_node.get("data", {}).get("nics", []):
            if nic.get("ip"):
                vm_ip = nic["ip"]
                break

    private_key = body.get("private_key", "")
    dom = _domain_name(project_id, vm_id)
    root_password = ""
    if vm_node:
        root_password = vm_node.get("data", {}).get("ciRootPassword", "")

    if method == "auto":
        methods = ["ssh", "console-text", "console", "serial"]
        force_tty = False
    else:
        methods = [method]
    errors = []

    for m in methods:
        try:
            if m == "ssh":
                if not vm_ip or not (password or private_key):
                    errors.append("ssh: no VM IP or credentials")
                    continue
                job_id = start_job(
                    host,
                    "/vm/ssh-exec",
                    {
                        "project_id": project_id,
                        "vm_ip": vm_ip,
                        "username": username,
                        "password": password,
                        "private_key": private_key,
                        "command": command,
                        "timeout": timeout,
                    },
                )
                job = wait_for_job(host, job_id, timeout=timeout + 30)
                if job["status"] == "completed":
                    result = job.get("result", {})
                    return {
                        "output": result.get("output", ""),
                        "error": result.get("error", ""),
                        "exit_code": result.get("exit_code", 0),
                        "method": "ssh",
                    }
                errors.append(f"ssh: {job.get('result', {}).get('error', 'failed')}")

            elif m == "serial":
                job_id = start_job(
                    host,
                    "/vm/serial-exec",
                    {
                        "domain_name": dom,
                        "username": username,
                        "password": password,
                        "command": command,
                        "timeout": timeout,
                    },
                )
                job = wait_for_job(host, job_id, timeout=90)
                if job["status"] == "completed":
                    result = job.get("result", {})
                    if result.get("output") or not result.get("error"):
                        return {
                            "output": result.get("output", ""),
                            "error": result.get("error", ""),
                            "method": "serial",
                        }
                errors.append(f"serial: {job.get('result', {}).get('error', 'failed')}")

            elif m in ("console", "console-text"):
                console_pass = root_password or password
                if not console_pass:
                    errors.append("console: no password available")
                    continue
                job_id = start_job(
                    host,
                    "/vm/console-exec",
                    {
                        "domain_name": dom,
                        "username": "root" if root_password else username,
                        "password": console_pass,
                        "command": command,
                        "timeout": timeout,
                        "force_tty": m == "console-text" or force_tty,
                    },
                )
                job = wait_for_job(host, job_id, timeout=timeout + 30)
                if job["status"] == "completed":
                    result = job.get("result", {})
                    if not result.get("error"):
                        return {
                            "output": result.get("output", ""),
                            "error": "",
                            "exit_code": result.get("exit_code"),
                            "method": "console",
                        }
                errors.append(
                    f"console: {job.get('result', {}).get('error', 'failed')}"
                )

        except TroshkadError as e:
            errors.append(f"{m}: {e}")
            if method != "auto":
                raise HTTPException(status_code=503, detail=f"{m} exec failed: {e}")

    raise HTTPException(
        status_code=503,
        detail="All exec methods failed: " + "; ".join(errors),
    )


def _resolve_vm_ssh_params(project, vm_id):
    """Resolve VM IP, username defaults, and password from topology."""
    vm_node = next(
        (n for n in (project.topology or {}).get("nodes", []) if n["id"] == vm_id),
        None,
    )
    if not vm_node:
        raise HTTPException(status_code=404, detail=f"VM {vm_id} not found in topology")

    vm_ip = ""
    for nic in vm_node.get("data", {}).get("nics", []):
        if nic.get("ip"):
            vm_ip = nic["ip"]
            break

    password = vm_node.get("data", {}).get("ciCloudUserPassword", "")
    return vm_node, vm_ip, password


@router.put("/{project_id}/vms/{vm_id}/files")
async def vm_upload_file(
    project_id: str,
    vm_id: str,
    file: UploadFile,
    remote_path: str = Query(..., description="Destination path on the VM"),
    mode: str = Query("0644", description="File permissions (octal)"),
    username: str = Query("cloud-user"),
    password: str = Query(""),
    private_key: str = Query(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a file to a VM via SCP."""
    project, host = _get_project_and_host(project_id, user, db)
    if project.state not in ("active", "stopped"):
        raise HTTPException(status_code=409, detail="Project must be active")

    vm_node, vm_ip, topo_password = _resolve_vm_ssh_params(project, vm_id)
    if not vm_ip:
        raise HTTPException(status_code=400, detail="VM has no IP address")
    pw = password or topo_password
    if not pw and not private_key:
        raise HTTPException(
            status_code=400, detail="No password or private key available for VM"
        )

    file_bytes = await file.read()
    try:
        result = troshkad_upload_to_vm(
            host,
            file_bytes,
            project_id,
            vm_ip,
            username,
            pw,
            remote_path,
            mode,
            private_key=private_key,
        )
        return result
    except TroshkadError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/{project_id}/vms/{vm_id}/files")
def vm_download_file(
    project_id: str,
    vm_id: str,
    remote_path: str = Query(..., description="Path of the file on the VM"),
    username: str = Query("cloud-user"),
    password: str = Query(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download a file from a VM via SCP."""
    project, host = _get_project_and_host(project_id, user, db)
    if project.state not in ("active", "stopped"):
        raise HTTPException(status_code=409, detail="Project must be active")

    vm_node, vm_ip, topo_password = _resolve_vm_ssh_params(project, vm_id)
    if not vm_ip:
        raise HTTPException(status_code=400, detail="VM has no IP address")
    pw = password or topo_password
    if not pw:
        raise HTTPException(status_code=400, detail="No password available for VM")

    try:
        file_bytes = troshkad_download_from_vm(
            host,
            project_id,
            vm_ip,
            username,
            pw,
            remote_path,
        )
    except TroshkadError as e:
        raise HTTPException(status_code=503, detail=str(e))

    import os

    filename = os.path.basename(remote_path)
    return Response(
        content=file_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/containers/{container_id}/logs")
def get_container_logs(
    project_id: str,
    container_id: str,
    tail: int = Query(500, description="Number of lines to retrieve from the end"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get logs from a container."""
    project, host = _get_project_and_host(project_id, user, db)
    container_name = f"troshka-{project_id[:8]}-{container_id[:8]}"

    try:
        job_id = start_job(
            host,
            "/containers/logs",
            {"container_name": container_name, "tail": tail},
        )
        result = wait_for_job(host, job_id, timeout=30)
        logs = result.get("result", {}).get("logs", "")
        return {"logs": logs, "container_name": container_name}
    except TroshkadError as e:
        logger.error("Failed to get logs for container %s: %s", container_name, e)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/{project_id}/containers/{container_id}/start")
def start_container(
    project_id: str,
    container_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)
    container_name = f"troshka-{project_id[:8]}-{container_id[:8]}"
    try:
        job_id = start_job(
            host, "/containers/start", {"container_name": container_name}
        )
        wait_for_job(host, job_id, timeout=30)
        return {"status": "started", "container_name": container_name}
    except TroshkadError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/{project_id}/containers/{container_id}/stop")
def stop_container(
    project_id: str,
    container_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)
    container_name = f"troshka-{project_id[:8]}-{container_id[:8]}"
    try:
        job_id = start_job(
            host, "/containers/stop", {"container_name": container_name, "timeout": 10}
        )
        wait_for_job(host, job_id, timeout=30)
        return {"status": "stopped", "container_name": container_name}
    except TroshkadError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/{project_id}/containers/{container_id}/restart")
def restart_container(
    project_id: str,
    container_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project, host = _get_project_and_host(project_id, user, db)
    container_name = f"troshka-{project_id[:8]}-{container_id[:8]}"
    try:
        job_id = start_job(
            host, "/containers/stop", {"container_name": container_name, "timeout": 10}
        )
        wait_for_job(host, job_id, timeout=30)
        job_id = start_job(
            host, "/containers/start", {"container_name": container_name}
        )
        wait_for_job(host, job_id, timeout=30)
        return {"status": "restarted", "container_name": container_name}
    except TroshkadError as e:
        raise HTTPException(status_code=503, detail=str(e))


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
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, cannot reconfigure"
        )
    if not project.host_id:
        raise HTTPException(status_code=400, detail="Project has no active deployment")

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.private_key or not host.ip_address:
        raise HTTPException(status_code=503, detail="Host not available")

    # Validate BMC network has at least one connected provisioner VM
    current = project.topology or {}
    bmc_net = next(
        (
            n
            for n in current.get("nodes", [])
            if n.get("type") == "networkNode"
            and n.get("data", {}).get("networkType") == "bmc"
        ),
        None,
    )
    if bmc_net:
        bmc_edges = [
            e
            for e in current.get("edges", [])
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
    diff = (
        diff_topologies(current, deployed)
        if deployed
        else {
            "added_vms": [],
            "removed_vms": [],
            "changed_vms": [],
            "added_networks": [],
            "removed_networks": [],
            "has_changes": False,
        }
    )
    if diff["added_networks"]:
        from app.services.vxlan import VNI_MAX, VNI_MIN, _get_all_used_vnis

        used_vnis = _get_all_used_vnis(db) | set(vni_map.values())
        next_vni = VNI_MIN
        for net_node in diff["added_networks"]:
            if (
                net_node.get("data", {}).get("subtype") == "network"
                and net_node.get("data", {}).get("networkType") != "bmc"
                and net_node["id"] not in vni_map
            ):
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
            _deploy_progress,
            _resolve_boot_devs,
            _vm_domain_name,
        )

        s = SessionLocal()
        try:
            proj = s.query(Project).filter_by(id=p_id).first()
            h = s.query(Host).filter_by(id=h_id).first()
            if not proj or not h:
                return

            current = proj.topology or {}
            deployed = proj.deployed_topology or {}
            vni_map = dict(proj.vni_map or {})
            diff = (
                diff_topologies(current, deployed)
                if deployed
                else {
                    "added_vms": [],
                    "removed_vms": [],
                    "changed_vms": [],
                    "added_networks": [],
                    "removed_networks": [],
                    "has_changes": False,
                }
            )

            errors = []

            # Sync EIPs before networking so DNAT rules have private IPs
            external_ips = current.get("externalIps", [])
            if external_ips:
                try:
                    from app.models.elastic_ip import ElasticIp
                    from app.models.provider import Provider
                    from app.services.eip_service import (
                        allocate_eip,
                        associate_eip,
                        sync_security_group_rules,
                    )

                    provider = (
                        s.query(Provider).filter_by(id=proj.provider_id).first()
                        if proj.provider_id
                        else None
                    )
                    if not provider and h.provider_id:
                        provider = s.query(Provider).filter_by(id=h.provider_id).first()
                    if provider:
                        for ext_ip in external_ips:
                            canvas_id = ext_ip.get("id", "")
                            existing = (
                                s.query(ElasticIp)
                                .filter_by(project_id=p_id, canvas_eip_id=canvas_id)
                                .first()
                            )
                            eip = existing or allocate_eip(s, provider, p_id, canvas_id)
                            if eip.state != "associated":
                                associate_eip(s, eip, h)
                            ext_ip["ip"] = eip.public_ip
                            ext_ip["_private_ip"] = eip.private_ip
                        import copy
                        import json

                        from sqlalchemy import text

                        new_topo = copy.deepcopy(current)
                        s.execute(
                            text(
                                "UPDATE projects SET topology = :topo WHERE id = :pid"
                            ),
                            {"topo": json.dumps(new_topo), "pid": p_id},
                        )
                        s.commit()
                        s.refresh(proj)

                        gw_node = next(
                            (
                                n
                                for n in current.get("nodes", [])
                                if n.get("type") == "networkNode"
                                and n.get("data", {}).get("subtype") == "gateway"
                                and n.get("data", {}).get("gatewayMode")
                                == "nat-portforward"
                            ),
                            None,
                        )
                        if gw_node:
                            desired_sg = [
                                {
                                    "project_id": p_id,
                                    "ext_port": int(pf["extPort"]),
                                    "protocol": "tcp",
                                }
                                for pf in gw_node.get("data", {}).get(
                                    "portForwards", []
                                )
                                if pf.get("extPort")
                            ]
                            sync_security_group_rules(s, provider, desired_sg)

                        if provider.type != "ec2" and gw_node:
                            from app.services.eip_service import allocate_transit_ports
                            from app.services.providers import get_provider_driver

                            driver = get_provider_driver(provider)
                            pf_list = gw_node.get("data", {}).get("portForwards", [])
                            eip_map = {}
                            for eip_obj in s.query(ElasticIp).filter_by(
                                project_id=p_id
                            ):
                                eip_map[eip_obj.canvas_eip_id] = eip_obj

                            for canvas_id, eip_obj in eip_map.items():
                                pf_for_eip = [
                                    pf
                                    for pf in pf_list
                                    if pf.get("extIpId") == canvas_id
                                ]
                                if not pf_for_eip:
                                    continue
                                eip_obj.port_map = None
                                s.commit()
                                port_map = allocate_transit_ports(
                                    s, eip_obj, h, pf_for_eip
                                )
                                driver.update_eip_ports(
                                    provider,
                                    h,
                                    eip_obj.allocation_id,
                                    [
                                        {
                                            "port": int(ep),
                                            "targetPort": tp,
                                            "name": f"pf-{i}",
                                        }
                                        for i, (ep, tp) in enumerate(port_map.items())
                                    ],
                                )
                                logger.info(
                                    "Reconfigure %s: updated EIP LB ports %s",
                                    p_id[:8],
                                    port_map,
                                )
                except Exception:
                    logger.exception("EIP sync failed during reconfigure %s", p_id[:8])
                    errors.append(
                        "EIP allocation/association failed — check server logs"
                    )

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

            # Only cache images and deploy metadata when VMs changed
            has_vm_changes = (
                diff.get("added_vms")
                or diff.get("removed_vms")
                or diff.get("changed_vms")
            )

            if has_vm_changes:
                _deploy_progress[p_id] = {"step": "downloading", "detail": "0%"}

                def _reconfig_dl_progress(downloaded, total):
                    pct = (
                        f"{int(downloaded / max(total, 1) * 100)}%"
                        if total > 0
                        else "..."
                    )
                    _deploy_progress[p_id] = {"step": "downloading", "detail": pct}

                cache_library_images(
                    current, h, s, progress_callback=_reconfig_dl_progress
                )
            if has_vm_changes:
                _deploy_progress[p_id] = {
                    "step": "cloud-init",
                    "detail": "deploying metadata service",
                }
                from app.services.deploy_service import _setup_metadata_via_troshkad

                try:
                    _setup_metadata_via_troshkad(h, p_id, current, vni_map)
                    logger.info("Reconfigure %s: metadata service deployed", p_id[:8])
                except Exception:
                    logger.exception(
                        "Reconfigure %s: metadata service deployment failed (non-fatal)",
                        p_id[:8],
                    )

                _setup_pxe_via_troshkad(h, current, vni_map, p_id)

            # Create BMC bridge if needed (must exist before VM restart)
            from app.services.deploy_service import _extract_bmc_config

            bmc_config = _extract_bmc_config(current, p_id)
            if bmc_config and has_vm_changes:
                net_data = bmc_config["bmc_network"]
                cidr = net_data.get("cidr", "192.168.100.0/24")
                try:
                    bj = start_job(
                        h,
                        "/bmc/create-bridge",
                        {
                            "project_id": p_id,
                            "bmc_cidr": cidr,
                            "bmc_gateway_ip": cidr.rsplit(".", 1)[0] + ".1",
                            "vms": [
                                {"bmc_ip": vm["bmc_ip"]} for vm in bmc_config["vms"]
                            ],
                        },
                    )
                    wait_for_job(h, bj, timeout=30)
                except TroshkadError:
                    logger.warning(
                        "Reconfigure %s: BMC bridge creation failed (non-fatal)",
                        p_id[:8],
                    )

            # Get storage pool for correct disk paths
            _pool = None
            if h.storage_pool_id:
                from app.models.storage_pool import StoragePool

                _pool = s.query(StoragePool).filter_by(id=h.storage_pool_id).first()
            vm_dir_path = _vm_dir(p_id, pool=_pool)

            for node in diff["removed_vms"]:
                dom = _vm_domain_name(p_id, node["id"])
                troshkad_undefine_vm(h, dom)
                # Remove disk files via troshkad
                try:
                    job_id = start_job(
                        h,
                        "/files/remove",
                        {
                            "paths": [
                                f"{vm_dir_path}/{node['id'][:8]}-{suffix}"
                                for suffix in ["*"]
                            ]
                        },
                    )
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
                nics = [
                    {"bridge": n["bridge"], "mac": n["mac"], "model": "virtio"}
                    for n in vm_networks
                ] or None

                # Build map of deployed disk library items for change detection
                dep_disk_libs = {}
                dep_disk_sizes = {}
                dep_vm_node = next(
                    (n for n in deployed.get("nodes", []) if n["id"] == vm["node_id"]),
                    None,
                )
                if dep_vm_node:
                    dep_disks = _find_vm_disks(vm["node_id"], deployed)
                    for dd in dep_disks:
                        dep_disk_libs[dd["node_id"]] = dd.get("library_item_id")
                        dep_disk_sizes[dd["node_id"]] = dd.get("size_gb", 0)

                from app.services.deploy_service import _image_cache_path

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
                            cdrom_list.append(
                                _image_cache_path(
                                    d["library_item_id"], "iso", pool=_pool
                                )
                            )
                        continue
                    path = _disk_path(
                        p_id, vm["node_id"], d["node_id"], d["format"], pool=_pool
                    )
                    disk_list.append(
                        {"path": path, "format": d["format"], "bus": d["bus"]}
                    )
                    old_lib = dep_disk_libs.get(d["node_id"])
                    new_lib = d.get("library_item_id")
                    image_changed = old_lib != new_lib and (old_lib or new_lib)
                    old_size = dep_disk_sizes.get(d["node_id"], 0)
                    size_grew = d["size_gb"] > old_size and old_size > 0
                    is_new_disk = (
                        d["node_id"] not in dep_disk_libs
                        and d["node_id"] not in dep_disk_sizes
                    )
                    if image_changed or size_grew or is_new_disk:
                        any_disk_changed = True
                    if image_changed:
                        files_to_remove.append(path)
                    backing = None
                    if d.get("source") == "library" and d.get("library_item_id"):
                        needs_library_download = True
                        backing = _image_cache_path(
                            d["library_item_id"], d["format"], pool=_pool
                        )
                    elif d.get("source") == "pattern" and d.get("patternId"):
                        backing = f"/var/lib/troshka/cache/patterns/{d['patternId']}/{d['patternDiskId']}.{d['format']}"
                    disks_to_create.append(
                        {
                            "path": path,
                            "size_gb": d["size_gb"],
                            "format": d["format"],
                            "backing_file": backing,
                        }
                    )
                    if size_grew and not image_changed:
                        disks_to_resize.append(
                            {"path": path, "new_size_gb": d["size_gb"]}
                        )

                if vm.get("cloud_init"):
                    cdrom_list.append(_seed_path(p_id, vm["node_id"], pool=_pool))

                if any_disk_changed:
                    if needs_library_download:
                        _deploy_progress[p_id] = {
                            "step": "checking images",
                            "detail": "",
                        }
                        cache_library_images(current, h, s)
                    # Remove changed disk files
                    if files_to_remove:
                        try:
                            job_id = start_job(
                                h, "/files/remove", {"paths": files_to_remove}
                            )
                            wait_for_job(h, job_id, timeout=30)
                        except TroshkadError as e:
                            logger.warning("Failed to remove old disk files: %s", e)
                    # Create new disks
                    for dc in disks_to_create:
                        params = {
                            "path": dc["path"],
                            "size_gb": dc["size_gb"],
                            "format": dc["format"],
                        }
                        if dc["backing_file"]:
                            params["backing_file"] = dc["backing_file"]
                        try:
                            job_id = start_job(h, "/disks/create", params)
                            wait_for_job(h, job_id, timeout=300)
                        except TroshkadError as e:
                            logger.warning(
                                "Failed to create disk %s: %s", dc["path"], e
                            )
                    # Resize disks
                    for dr in disks_to_resize:
                        try:
                            job_id = start_job(h, "/disks/resize", dr)
                            wait_for_job(h, job_id, timeout=60)
                        except TroshkadError as e:
                            logger.warning(
                                "Failed to resize disk %s: %s", dr["path"], e
                            )

                current_cfg = troshkad_get_vm_config(h, dom)
                if not current_cfg:
                    vm_node = next(
                        (
                            n
                            for n in current.get("nodes", [])
                            if n["id"] == vm["node_id"]
                        ),
                        None,
                    )
                    if vm_node:
                        diff["added_vms"].append(vm_node)
                    continue

                desired_nics = (
                    [{"bridge": n["bridge"], "mac": n["mac"]} for n in vm_networks]
                    if vm_networks
                    else []
                )
                current_bridges = sorted(n["bridge"] for n in current_cfg["nics"])
                desired_bridges = sorted(n["bridge"] for n in desired_nics)
                desired_disks = [d["path"] for d in disk_list]
                if (
                    current_cfg["boot_devs"] == boot_devs
                    and current_cfg["vcpus"] == vm["vcpus"]
                    and current_cfg["ram_mb"] == vm["ram_gb"] * 1024
                    and current_bridges == desired_bridges
                    and current_cfg["disks"] == desired_disks
                    and sorted(current_cfg.get("cdroms", [])) == sorted(cdrom_list)
                ):
                    logger.debug(
                        "Reconfigure %s: VM %s unchanged, skipping",
                        p_id[:8],
                        vm["name"],
                    )
                    continue

                logger.info(
                    "Reconfigure %s: VM %s changed — boot_devs:%s vcpus:%s ram:%s bridges:%s disks:%s cdroms:%s",
                    p_id[:8],
                    vm["name"],
                    current_cfg["boot_devs"] != boot_devs,
                    current_cfg["vcpus"] != vm["vcpus"],
                    current_cfg["ram_mb"] != vm["ram_gb"] * 1024,
                    current_bridges != desired_bridges,
                    current_cfg["disks"] != desired_disks,
                    sorted(current_cfg.get("cdroms", [])) != sorted(cdrom_list),
                )
                _deploy_progress[p_id] = {"step": "reconfiguring", "detail": vm["name"]}
                disk_only_change = (
                    current_cfg["disks"] != desired_disks
                    and current_cfg["boot_devs"] == boot_devs
                    and current_cfg["vcpus"] == vm["vcpus"]
                    and current_cfg["ram_mb"] == vm["ram_gb"] * 1024
                    and current_bridges == desired_bridges
                    and sorted(current_cfg.get("cdroms", [])) == sorted(cdrom_list)
                )
                needs_restart = (
                    vm["node_id"] in restart_vm_ids
                    or current_cfg["boot_devs"] != boot_devs
                    or current_cfg["vcpus"] != vm["vcpus"]
                    or current_cfg["ram_mb"] != vm["ram_gb"] * 1024
                    or current_bridges != desired_bridges
                ) and not disk_only_change
                try:
                    troshkad_reconfigure_vm(
                        h,
                        dom,
                        boot_devs=boot_devs,
                        vcpus=vm["vcpus"],
                        ram_mb=vm["ram_gb"] * 1024,
                        nics=nics,
                        disks=disk_list,
                        cdroms=cdrom_list,
                        restart=needs_restart,
                    )
                except TroshkadError as e:
                    errors.append(f"Failed to reconfigure {dom}: {e}")

            if diff["added_vms"]:
                _deploy_progress[p_id] = {"step": "downloading", "detail": "0%"}

                def _progress(downloaded, total):
                    pct = (
                        f"{int(downloaded / max(total, 1) * 100)}%"
                        if total > 0
                        else "..."
                    )
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
                        no_auto_start = {
                            e["vmId"]
                            for e in current.get("startOrder", [])
                            if e.get("autoStart") is False
                        }
                        if vm_node["id"] not in no_auto_start:
                            vm_name = _vm_domain_name(p_id, vm_node["id"])
                            job_id = start_job(
                                h, "/vms/start", {"domain_name": vm_name}
                            )
                            wait_for_job(h, job_id, timeout=60)
                    except (TroshkadError, RuntimeError) as e:
                        errors.append(f"Failed to add VM {vm_node['id'][:8]}: {e}")

            from app.services.placement import sync_host_capacity

            sync_host_capacity(s, h)

            # BMC setup/teardown during reconfigure
            from app.services.deploy_service import (
                _extract_bmc_config,
                _setup_bmc_via_troshkad,
                _teardown_bmc_via_troshkad,
            )

            bmc_config = _extract_bmc_config(current, p_id)
            deployed_had_bmc = any(
                n.get("type") == "networkNode"
                and n.get("data", {}).get("networkType") == "bmc"
                for n in deployed.get("nodes", [])
            )
            if deployed_had_bmc:
                try:
                    _teardown_bmc_via_troshkad(h, p_id)
                except Exception:
                    logger.warning(
                        "Reconfigure %s: BMC teardown failed (non-fatal)", p_id[:8]
                    )
            if bmc_config:
                try:
                    bmc_result = _setup_bmc_via_troshkad(h, p_id, bmc_config)
                    if bmc_result is not True:
                        errors.append(f"BMC setup failed: {bmc_result}")
                except Exception:
                    logger.warning(
                        "Reconfigure %s: BMC setup failed (non-fatal)", p_id[:8]
                    )
                    errors.append("BMC setup failed — check server logs")

            s.refresh(proj)
            final_topo = proj.topology or {}

            import copy

            proj.state = "active"
            if not errors:
                # Store BMC addresses in deployed topology
                deployed_topo = copy.deepcopy(final_topo)
                if bmc_config:
                    deployed_topo["bmc"] = {
                        "username": bmc_config["bmc_network"].get(
                            "bmcUsername", "admin"
                        ),
                        "password": bmc_config["bmc_network"].get(
                            "bmcPassword", "password"
                        ),
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
            notify_project(
                p_id,
                {
                    "type": "project-state",
                    "state": "active",
                    "deploy_error": proj.deploy_error,
                },
            )
            try:
                from app.services.troshkad_client import get_all_vm_states

                batch = get_all_vm_states(h) or {}
                vm_states = {}
                for node in (current or {}).get("nodes", []):
                    if node.get("type") != "vmNode":
                        continue
                    dom = _vm_domain_name(p_id, node["id"])
                    raw = batch.get(dom, "unknown")
                    vm_states[node["id"]] = (
                        "running"
                        if raw == "running"
                        else "stopped"
                        if raw == "shut_off"
                        else raw
                    )
                notify_project(
                    p_id, {"type": "vm-state", "states": vm_states, "progress": {}}
                )
            except Exception:
                pass
            logger.info(
                "Reconfigure %s complete%s",
                p_id[:8],
                f" with errors: {errors}" if errors else "",
            )
        except Exception:
            logger.exception("Reconfigure %s failed", p_id[:8])
            proj = s.query(Project).filter_by(id=p_id).first()
            if proj:
                proj.state = "error"
                s.commit()
            _deploy_progress.pop(p_id, None)
        finally:
            s.close()

    threading.Thread(
        target=_do_reconfigure, daemon=True, name=f"reconfig-{p_id[:8]}"
    ).start()
    return {"status": "reconfiguring"}


@router.post("/{project_id}/vms/{vm_id}/redeploy")
def redeploy_vm(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Destroy and recreate a single VM in a background thread."""
    project, host = _get_project_and_host(project_id, user, db, check_disk=True)
    _check_library_items_ready(project.topology or {}, db)

    p_id = project.id
    host_id = host.id
    target_vm_id = vm_id

    import threading

    def _do_redeploy():
        from app.core.database import SessionLocal
        from app.services.deploy_service import (
            _get_host_pool,
            _vm_domain_name,
        )

        s = SessionLocal()
        try:
            proj = s.query(Project).filter_by(id=p_id).first()
            h = s.query(Host).filter_by(id=host_id).first()
            if not proj or not h:
                return

            dom = _vm_domain_name(p_id, target_vm_id)
            _vm_dir(p_id)
            topology = proj.topology
            vni_map = proj.vni_map or {}

            was_running = troshkad_get_vm_state(h, dom)["state"] == "running"
            troshkad_undefine_vm(h, dom, remove_storage=False)

            _redeploy_progress[dom] = {"step": "preparing", "detail": ""}
            # Remove old disk files via troshkad
            # Build list of known files for this VM
            vm_disks_to_remove = _find_vm_disks(target_vm_id, topology or {})
            paths_to_remove = []
            for d in vm_disks_to_remove:
                if d["format"] != "iso":
                    paths_to_remove.append(
                        _disk_path(p_id, target_vm_id, d["node_id"], d["format"])
                    )
            paths_to_remove.append(_seed_path(p_id, target_vm_id))
            try:
                job_id = start_job(h, "/files/remove", {"paths": paths_to_remove})
                wait_for_job(h, job_id, timeout=15)
            except TroshkadError as e:
                logger.warning("Redeploy %s: failed to remove old files: %s", dom, e)

            vm_node = next(
                (
                    n
                    for n in (topology or {}).get("nodes", [])
                    if n["id"] == target_vm_id and n.get("type") == "vmNode"
                ),
                None,
            )
            if not vm_node:
                logger.warning(
                    "Redeploy %s: node not found in topology", target_vm_id[:8]
                )
                _redeploy_progress.pop(dom, None)
                return

            edges = (topology or {}).get("edges", [])
            vm_connected_ids = set()
            for edge in edges:
                src, tgt = edge.get("source"), edge.get("target")
                if src == target_vm_id:
                    vm_connected_ids.add(tgt)
                elif tgt == target_vm_id:
                    vm_connected_ids.add(src)
            vm_topo = {
                "nodes": [
                    n
                    for n in (topology or {}).get("nodes", [])
                    if n["id"] in vm_connected_ids
                ]
            }

            _redeploy_progress[dom] = {"step": "downloading", "detail": "0%"}

            def _progress(downloaded, total):
                pct = (
                    f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
                )
                _redeploy_progress[dom] = {"step": "downloading", "detail": pct}

            cache_library_images(vm_topo, h, s, progress_callback=_progress)

            _setup_pxe_via_troshkad(h, topology, vni_map, p_id)

            pool = _get_host_pool(h, s)
            _redeploy_progress[dom] = {
                "step": "creating",
                "detail": "cloud-init seed ISO",
            }
            vm_only_topo = {"nodes": [vm_node], "edges": []}
            _create_seed_isos_via_troshkad(h, p_id, vm_only_topo, pool)

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
            disk_cache = "none" if pool and pool.mode.startswith("shared") else None
            vm_disks = _find_vm_disks(target_vm_id, topology or {})
            _create_vm_disks_via_troshkad(h, p_id, vm_data, vm_disks, pool)
            _create_vm_via_troshkad(
                h, p_id, vm_data, topology or {}, vni_map, pool, disk_cache
            )

            should_start = was_running or vdata.get("powerOnAtDeploy", True)
            if should_start:
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

    threading.Thread(
        target=_do_redeploy, daemon=True, name=f"redeploy-{p_id[:8]}"
    ).start()
    return {"status": "redeploying"}


@router.post("/{project_id}/vms/{vm_id}/cancel-redeploy")
def cancel_redeploy(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, cannot redeploy"
        )

    _check_library_items_ready(project.topology or {}, db)

    # Destroy existing and release capacity
    if project.host_id:
        old_host_id = project.host_id
        old_host = db.query(Host).filter_by(id=old_host_id).first()
        if not old_host or not old_host.ip_address:
            raise HTTPException(
                status_code=503,
                detail="Host not reachable — cannot destroy existing VMs. Stop the project first or wait for the host to come online.",
            )
        destroy_project_sync(
            {
                "project_id": project.id,
                "host_id": project.host_id,
                "vni_map": project.vni_map or {},
                "topology": project.deployed_topology or project.topology or {},
                "dns_provider_id": project.dns_provider_id,
                "domain": project.domain,
            }
        )
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

    threading.Thread(
        target=deploy_project_async,
        args=(project.id,),
        daemon=True,
        name=f"deploy-{project.id[:8]}",
    ).start()

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
        destroy_project_sync(
            {
                "project_id": project.id,
                "host_id": project.host_id,
                "vni_map": project.vni_map or {},
                "topology": project.deployed_topology or project.topology or {},
                "dns_provider_id": project.dns_provider_id,
                "domain": project.domain,
            }
        )

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

    # Capture all data needed for cleanup BEFORE deleting the DB row
    if project.host_id and project.state in ("active", "stopped", "error"):
        import copy
        import threading

        destroy_ctx = {
            "project_id": project.id,
            "host_id": project.host_id,
            "vni_map": copy.deepcopy(project.vni_map or {}),
            "topology": copy.deepcopy(
                project.deployed_topology or project.topology or {}
            ),
            "dns_provider_id": project.dns_provider_id,
            "domain": project.domain,
        }
        threading.Thread(
            target=destroy_project_sync,
            args=(destroy_ctx,),
            daemon=True,
            name=f"destroy-{project.id[:8]}",
        ).start()

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
            ]
            + [
                {"id": f"dp-{uuid_mod.uuid4()}"}
                for _ in range(
                    max(
                        0,
                        len(vm_config.get("disks", []))
                        - len(vm_config.get("diskControllers", [])),
                    )
                )
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

    vm_data: dict[str, Any] = vm_node["data"]  # type: ignore[assignment]
    disks = vm_config.get("disks", [])
    dc_list = vm_data["diskControllers"]
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
                "libraryItemId": disk_info.get("libraryItemId"),
                "libraryItemName": disk_info.get("libraryItemName"),
                "icon": (
                    "\U0001f6e2" if disk_info.get("format") != "iso" else "\U0001f4bf"
                ),
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
            "style": {
                "stroke": "rgba(251,191,36,0.6)",
                "strokeWidth": 2,
                "strokeDasharray": "4 4",
            },
        }
        topology["edges"].append(edge)
        boot_devices.append(disk_id)

    if boot_devices:
        vm_data["bootDevices"] = boot_devices

    networks_info = vm_config.get("networks", [])
    nic_list = vm_data["nics"]
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
            "style": {
                "stroke": "rgba(56,189,248,0.6)",
                "strokeWidth": 2,
                "strokeDasharray": "6 4",
            },
        }
        topology["edges"].append(edge)
        nic_list = nic_list[1:]

    project.topology = topology
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "topology")
    db.commit()
    db.refresh(project)
    return project


class MigrateRequest(PydanticBaseModel):
    target_host_id: str


@router.post("/{project_id}/migrate")
def migrate_project_endpoint(
    project_id: str,
    body: MigrateRequest,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    from app.services.migration_service import migrate_project, validate_migration

    project = db.query(Project).get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    if not project.host_id:
        raise HTTPException(400, "Project has no assigned host")

    host_id: str = project.host_id
    errors = validate_migration(db, project_id, host_id, body.target_host_id)
    if errors:
        raise HTTPException(400, "; ".join(errors))

    migrate_project(project_id, host_id, body.target_host_id)
    return {
        "status": "migrating",
        "project_id": project_id,
        "target_host_id": body.target_host_id,
    }
