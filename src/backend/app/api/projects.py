import datetime
import logging
import uuid as uuid_mod
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.core.logging_utils import sanitize_log
from app.models.host import Host
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.deploy_service import (  # noqa: F401
    _create_seed_isos_via_troshkad,
    _create_vm_disks_via_troshkad,
    _create_vm_via_troshkad,
    _setup_networks_via_troshkad,
    _setup_pxe_via_troshkad,
    _teardown_networks_via_troshkad,
    cache_library_images,
    deploy_project_async,
    destroy_project_sync,
    start_project_async,
    stop_project_async,
)
from app.services.deploy_topology import (  # noqa: F401
    _disk_path,
    _extract_vms,
    _find_vm_disks,
    _find_vm_networks,
    _seed_path,
    _vm_dir,
    diff_topologies,
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

_VMS_START_PATH = "/vms/start"
_FILES_REMOVE_PATH = "/files/remove"
_TROSHKA_DOMAIN = "troshka.redhat.com"

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]

_PROJECT_NOT_FOUND = "Project not found"
_ACCESS_DENIED = "Access denied"
_HOST_NOT_AVAILABLE = "Host not available"
_KUBEVIRT_API = "kubevirt.io"
_OCP_LOCAL = "ocp.local"
_PROJECT_MUST_BE_ACTIVE = "Project must be active"


def _get_k8s_clients_for_kubevirt(provider):
    from app.services.providers.kubevirt import _get_k8s_clients

    return _get_k8s_clients(provider)


def _kubevirt_project_ns(provider, project_id):
    from app.services.providers.kubevirt import _project_ns

    return _project_ns(provider, project_id)


def _resolve_deploy_progress(project) -> dict | None:
    """Return deploy progress data for a project in a transitional state."""
    if project.state not in ("deploying", "reconfiguring", "starting", "stopping"):
        return None
    from app.services.deploy_service import _get_deploy_progress_data

    dp = _get_deploy_progress_data(project.id)
    if dp:
        return dp
    if project.deploy_progress:
        dp = project.deploy_progress
    if project.state == "deploying":
        from app.core.redis import get_job_info

        job_info = get_job_info(project.id)
        if job_info and job_info.get("status") == "queued":
            return {
                "step": "queued",
                "detail": f"#{job_info.get('queue_position', '?')} of {job_info.get('queue_length', '?')}",
            }
    return dp


def _resolve_provider_type(project) -> str | None:
    """Look up the provider type for a project's host via its SA session."""
    if not project.host_id:
        return None
    from sqlalchemy.orm import Session as _S

    from app.models.host import Host
    from app.models.provider import Provider

    s: _S = object.__getattribute__(project, "_sa_instance_state").session
    if not s:
        return None
    h = s.get(Host, project.host_id)
    if not h or not h.provider_id:
        return None
    prov = s.get(Provider, h.provider_id)
    return prov.type if prov else None


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
        "guest_exec_enabled": project.guest_exec_enabled,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }
    dp = _resolve_deploy_progress(project)
    if dp:
        result["deploy_progress"] = dp

    from app.services.ws_pubsub import get_cached_vm_states

    cached_states = get_cached_vm_states(project.id)
    if cached_states:
        result["vm_states"] = cached_states

    deployed_topo = project.deployed_topology or {}
    bmc_data = deployed_topo.get("bmc")
    if bmc_data:
        result["bmc"] = bmc_data
    prov_type = _resolve_provider_type(project)
    if prov_type:
        result["provider_type"] = prov_type
    return result


def _enrich_project_response(p, hosts_by_id, provs_by_id, owners_by_id):
    """Build a ProjectResponse with owner/host/provider/progress data."""
    from app.core.redis import get_job_info
    from app.services.deploy_service import _get_deploy_progress_data

    resp = ProjectResponse.model_validate(p)
    owner = owners_by_id.get(p.owner_id)
    if owner:
        resp.owner_email = owner.email
    h = hosts_by_id.get(p.host_id) if p.host_id else None
    if h:
        resp.host_instance_id = h.instance_id
        resp.host_ip = h.ip_address
        prov = provs_by_id.get(h.provider_id) if h.provider_id else None
        if prov:
            resp.host_provider_name = prov.name
            resp.host_provider_type = prov.type
    dp = _get_deploy_progress_data(p.id)
    if dp:
        resp.deploy_progress = dp
    elif p.state == "deploying":
        job_info = get_job_info(p.id)
        if job_info and job_info.get("status") == "queued":
            resp.deploy_progress = {
                "step": "queued",
                "detail": f"#{job_info['queue_position']} of {job_info['queue_length']}",
                "queue_position": job_info["queue_position"],
                "queue_length": job_info["queue_length"],
            }
    return resp


@router.get("/", response_model=list[ProjectResponse])
def list_projects(
    user: CurrentUser,
    db: DbSession,
    skip: int = 0,
    limit: int = 200,
    guid: str | None = None,
):
    if user.role == "admin":
        query = db.query(Project)
    else:
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

    owner_ids = {p.owner_id for p in projects}
    owners_by_id = {}
    if owner_ids:
        owners = db.query(User).filter(User.id.in_(owner_ids)).all()
        owners_by_id = {u.id: u for u in owners}

    return [
        _enrich_project_response(p, hosts_by_id, provs_by_id, owners_by_id)
        for p in projects
    ]


@router.post("/", response_model=ProjectResponse, status_code=201, responses={409: {}})
def create_project(
    body: ProjectCreate,
    user: CurrentUser,
    db: DbSession,
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
def list_topology_templates(user: CurrentUser):
    from app.services.template_loader import list_yaml_templates

    return list_yaml_templates()


@router.post("/auto-layout")
def auto_layout_topology(body: dict, user: CurrentUser):
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


def _resolve_bastion_image(db, user, bastion_image_id):
    """Look up the bastion image by ID or fall back to the user's default."""
    from app.models.library import Library, LibraryItem

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
        return {
            "id": item.id,
            "name": item.name,
            "size_gb": max(1, (item.size_bytes or 0) // (1024**3)),
        }
    return None


def _resolve_bastion_iso(db, user, bastion_iso_id):
    """Look up the bastion ISO by ID or fall back to the user's default."""
    from app.models.library import Library, LibraryItem

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
        return {
            "id": iso_item.id,
            "name": iso_item.name,
            "size_bytes": iso_item.size_bytes or 0,
        }
    return None


def _resolve_ssh_keys(db, user, body):
    """Resolve SSH public key and key IDs from body or user's stored keys."""
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
    return ssh_pub_key, ssh_key_ids, ssh_keys


def _resolve_pull_secret(user):
    """Decrypt and return the user's OCP pull secret, or empty string."""
    if user.ocp_pull_secret:
        from app.core.encryption import decrypt

        return decrypt(user.ocp_pull_secret)
    return ""


def _apply_bastion_cloud_init(
    topology, bastion_image, bastion_iso, common_password, ssh_key_ids, ssh_keys
):
    """Attach bastion image/ISO and set cloud-init for non-OCP templates."""
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


def _parse_clock_target(clock_target_str):
    """Parse a clock target string or datetime into a datetime object."""
    if not clock_target_str:
        return None
    from datetime import datetime

    if isinstance(clock_target_str, str):
        return datetime.fromisoformat(clock_target_str.replace("Z", "+00:00"))
    return clock_target_str


def _resolve_template_source(body):
    """Resolve template from either template_yaml or template_id."""
    from app.services.template_loader import resolve_inline_template, resolve_template

    template_yaml = body.get("template_yaml")
    template_id = body.get("template_id")

    if template_yaml:
        resolved = resolve_inline_template(template_yaml)
        return resolved, resolved.get("name", "inline")
    if template_id:
        try:
            resolved = resolve_template(template_id)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"Template '{template_id}' not found"
            )
        return resolved, template_id
    raise HTTPException(
        status_code=400, detail="template_id or template_yaml is required"
    )


@router.post("/from-template", status_code=201, responses={400: {}, 404: {}})
def create_project_from_template(
    body: dict,
    user: CurrentUser,
    db: DbSession,
):
    from app.services.template_loader import generate_topology_from_template

    resolved, template_id = _resolve_template_source(body)

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
    bastion_image = _resolve_bastion_image(db, user, body.get("bastion_image_id"))
    bastion_iso = _resolve_bastion_iso(db, user, body.get("bastion_iso_id"))
    ssh_pub_key, ssh_key_ids, ssh_keys = _resolve_ssh_keys(db, user, body)
    pull_secret_json = _resolve_pull_secret(user)

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
        _apply_bastion_cloud_init(
            topology,
            bastion_image,
            bastion_iso,
            common_password,
            ssh_key_ids,
            ssh_keys,
        )
    else:
        customize_ocp(
            topology,
            template_id,
            {
                "cluster_name": body.get("cluster_name", "ocp"),
                "base_domain": body.get("base_domain", _OCP_LOCAL),
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
    base_domain = body.get("base_domain", _OCP_LOCAL)
    ocp_version = body.get("ocp_version", "")
    if ocp_version:
        desc_parts.append(f"OCP {ocp_version}")
    desc_parts.append(f"API: api.{cluster_name}.{base_domain}")

    from app.services.deploy_topology import (
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

    ct = _parse_clock_target(body.get("clock_target") or resolved.get("clock_target"))
    if ct:
        project.clock_target = ct

    db.add(project)
    db.commit()
    db.refresh(project)
    return {"id": project.id, "name": project.name}


def _validate_template_yaml(template_yaml):
    """Validate that template_yaml is present and has required sections."""
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


def _find_library_item(db, user_id, item_id, item_name, label, missing):
    """Look up a library item by ID, falling back to name. Appends to missing if not found."""
    from app.models.library import Library, LibraryItem

    if item_id:
        item = (
            db.query(LibraryItem)
            .join(Library)
            .filter(LibraryItem.id == item_id, Library.owner_id == user_id)
            .first()
        )
        if item:
            return item
    if item_name:
        item = (
            db.query(LibraryItem)
            .join(Library)
            .filter(LibraryItem.name == item_name, Library.owner_id == user_id)
            .first()
        )
        if item:
            return item
    if item_id or item_name:
        missing.append(f"{label}: '{item_name or item_id}' not found")
    return None


def _resolve_vm_library_refs(db, user_id, vm_name, vm_cfg, missing):
    """Resolve all library item references for a single VM definition."""
    for di, disk_cfg in enumerate(vm_cfg.get("disks", [])):
        item = _find_library_item(
            db,
            user_id,
            disk_cfg.get("library_item_id"),
            disk_cfg.get("library_item_name"),
            f"VM '{vm_name}' disk {di}",
            missing,
        )
        if item:
            disk_cfg["library_item_id"] = item.id
            disk_cfg["library_item_name"] = item.name
    iso_id = vm_cfg.get("pxe_boot_iso_id")
    if iso_id:
        item = _find_library_item(
            db,
            user_id,
            iso_id,
            vm_cfg.get("pxe_boot_iso_name"),
            f"VM '{vm_name}' PXE boot ISO",
            missing,
        )
        if item:
            vm_cfg["pxe_boot_iso_id"] = item.id
            vm_cfg["pxe_boot_iso_name"] = item.name
    for ii, iso_cfg in enumerate(vm_cfg.get("isos", [])):
        item = _find_library_item(
            db,
            user_id,
            iso_cfg.get("library_item_id"),
            iso_cfg.get("library_item_name"),
            f"VM '{vm_name}' ISO {ii}",
            missing,
        )
        if item:
            iso_cfg["library_item_id"] = item.id
            iso_cfg["library_item_name"] = item.name


def _resolve_template_library_items(db, user, vms_def):
    """Resolve and validate all library item references in the template VMs."""
    missing = []
    for vm_name, vm_cfg in vms_def.items():
        _resolve_vm_library_refs(db, user.id, vm_name, vm_cfg, missing)
    if missing:
        raise HTTPException(
            status_code=400,
            detail="Library items not found:\n" + "\n".join(missing),
        )


@router.post(
    "/{project_id}/import-template", responses={400: {}, 403: {}, 404: {}, 409: {}}
)
def import_template(
    project_id: str,
    body: dict,
    user: CurrentUser,
    db: DbSession,
):
    from app.services.template_loader import (
        generate_topology_from_template,
        resolve_inline_template,
    )

    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    if project.state != "draft":
        raise HTTPException(
            status_code=409, detail="Can only import template on draft projects"
        )

    template_yaml = body.get("template_yaml")
    _validate_template_yaml(template_yaml)
    assert template_yaml is not None
    _resolve_template_library_items(db, user, template_yaml.get("vms", {}))

    try:
        resolved = resolve_inline_template(template_yaml)
        topology = generate_topology_from_template(resolved)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid template: {e}")

    from app.services.deploy_topology import (
        validate_topology_ips,
        validate_topology_names,
    )

    topo_errors = validate_topology_names(topology) + validate_topology_ips(topology)
    if topo_errors:
        raise HTTPException(
            status_code=400,
            detail="Template produces duplicate names: " + "; ".join(topo_errors),
        )
    _enforce_single_bastion_browser(topology)

    project.topology = topology

    ct = _parse_clock_target(resolved.get("clock_target"))
    if ct:
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
        elif mode == "custom" and custom and "bmc_password" in net_cfg:
            net_cfg["bmc_password"] = custom
    for vm_cfg in result.get("vms", {}).values():
        if mode == "none":
            vm_cfg.pop("cloud_user_password", None)
        elif mode == "custom" and custom and "cloud_user_password" in vm_cfg:
            vm_cfg["cloud_user_password"] = custom


def _strip_library_ids_from_export(result):
    """Remove library_item_id fields from exported template VMs."""
    for vm_cfg in result.get("vms", {}).values():
        for disk in vm_cfg.get("disks", []):
            disk.pop("library_item_id", None)
        for iso in vm_cfg.get("isos", []):
            iso.pop("library_item_id", None)
        vm_cfg.pop("pxe_boot_iso_id", None)


def _build_export_header(pw_mode):
    """Return the YAML comment header based on password mode."""
    if pw_mode == "none":
        return "# Troshka infra_template export\n# Passwords omitted — set them before deploying.\n\n"
    return "# Troshka infra_template export\n# WARNING: Passwords are stored in plain text.\n\n"


@router.post("/{project_id}/export-template", responses={403: {}, 404: {}})
def export_template(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
    body: dict | None = None,
):
    from app.services.template_loader import export_topology_to_template

    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

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
            "base_domain": ocp_meta.get("baseDomain", _OCP_LOCAL),
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
        _strip_library_ids_from_export(result)

    import yaml  # type: ignore[import-untyped]
    from fastapi.responses import Response

    yaml_str = yaml.dump(result, default_flow_style=False, sort_keys=False)
    header = _build_export_header(pw_mode)
    return Response(content=header + yaml_str, media_type="text/yaml")


@router.get("/{project_id}", responses={403: {}, 404: {}})
def get_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    return _project_response_dict(project)


@router.get("/{project_id}/deploy-progress", responses={403: {}, 404: {}})
def get_deploy_progress(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    from app.services.deploy_service import get_deploy_progress as _get_dp

    progress = _get_dp(project_id)
    return {"state": project.state, "progress": progress}


@router.get("/{project_id}/kubeconfigs", responses={403: {}, 404: {}})
def list_kubeconfigs(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    """List available kubeconfigs for a project's recerted VMs."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    topo = project.deployed_topology or project.topology or {}
    configs = []
    for node in topo.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if data.get("ocpKubeconfig"):
            configs.append(
                {
                    "vm_name": data.get("label") or data.get("name", "vm"),
                    "vm_id": node["id"],
                }
            )
    return configs


def _find_kubeconfig_content(topo: dict, vm: str | None) -> str | None:
    """Search topology nodes for a kubeconfig matching the given VM name."""
    for node in topo.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        name = data.get("label") or data.get("name", "")
        if vm and name != vm:
            continue
        if data.get("ocpKubeconfig"):
            return data["ocpKubeconfig"]
    return None


@router.get("/{project_id}/kubeconfig", responses={403: {}, 404: {}})
def get_kubeconfig(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
    vm: str | None = None,
):
    """Download kubeconfig for a project's OCP cluster.

    Reads from the deployed_topology node data (stored during recert).
    Optional ?vm=name to get a specific VM's kubeconfig.
    """
    from fastapi.responses import Response

    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    topo = project.deployed_topology or project.topology or {}
    kc_content = _find_kubeconfig_content(topo, vm)
    if not kc_content:
        raise HTTPException(
            status_code=404,
            detail=f"Kubeconfig not found for {vm or 'default'}",
        )

    filename = f"kubeconfig-{vm}.yaml" if vm else "kubeconfig.yaml"
    return Response(
        content=kc_content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _recompute_auto_stop_timer(project, fields):
    """Recompute auto-stop expiry when auto_stop_minutes changes."""
    if fields["auto_stop_minutes"] is None:
        project.auto_stop_started_at = None
        project.auto_stop_expires_at = None
        project.auto_stop_warned = False
        return
    now = datetime.datetime.now(datetime.UTC)
    if not project.auto_stop_started_at and project.state == "active":
        project.auto_stop_started_at = now
    if project.auto_stop_started_at:
        project.auto_stop_expires_at = (
            project.auto_stop_started_at
            + datetime.timedelta(minutes=project.auto_stop_minutes or 0)
        )
    project.auto_stop_warned = False


def _recompute_auto_delete_timer(project, fields):
    """Recompute auto-delete expiry when auto_delete_minutes changes."""
    if fields["auto_delete_minutes"] is None:
        project.auto_delete_started_at = None
        project.lifetime_expires_at = None
        project.auto_delete_warned = False
        return
    now = datetime.datetime.now(datetime.UTC)
    if not project.auto_delete_started_at and project.state != "draft":
        project.auto_delete_started_at = now
    if project.auto_delete_started_at:
        project.lifetime_expires_at = (
            project.auto_delete_started_at
            + datetime.timedelta(minutes=project.auto_delete_minutes or 0)
        )
    project.auto_delete_warned = False


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    responses={400: {}, 403: {}, 404: {}},
)
def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    fields = body.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(project, field, value)

    if "auto_stop_minutes" in fields:
        _recompute_auto_stop_timer(project, fields)
    if "auto_delete_minutes" in fields:
        _recompute_auto_delete_timer(project, fields)

    # Live clock adjustment
    if "clock_target" in fields and project.state == "active":
        from app.services.clock_service import adjust_clocks_async

        adjust_clocks_async(project_id)

    if "topology" in fields:
        _enforce_single_bastion_browser(fields["topology"])

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


@router.post("/{project_id}/extend-timer", responses={400: {}, 403: {}, 404: {}})
def extend_timer(
    project_id: str,
    body: ExtendTimerRequest,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

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


def _validate_bmc_network(topology: dict):
    """Raise if a BMC network exists but has no connected VMs."""
    bmc_network = None
    for node in topology.get("nodes", []):
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("networkType") == "bmc"
        ):
            bmc_network = node
            break
    if not bmc_network:
        return
    bmc_edges = [
        e
        for e in topology.get("edges", [])
        if e.get("source") == bmc_network["id"] or e.get("target") == bmc_network["id"]
    ]
    if not bmc_edges:
        raise HTTPException(
            status_code=400,
            detail="BMC network requires at least one connected VM to act as a provisioner",
        )


def _validate_deploy_pool_and_host(db, user, storage_pool_id, host_id):
    """Validate admin-specified pool and host for deployment."""
    if (storage_pool_id or host_id) and user.role != "admin":
        raise HTTPException(
            status_code=403, detail="Only admins can select a storage pool or host"
        )
    if storage_pool_id:
        from app.models.storage_pool import StoragePool

        pool = db.get(StoragePool, storage_pool_id)
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


def _setup_multi_host_placement(project, result, db):
    """Configure project for multi-host deployment."""
    project.state = "deploying"
    project.mesh_network_host_id = result["network_host_id"]
    project.host_id = result["network_host_id"]  # backward compat
    project.vni_map = result["vni_map"]
    # Flatten {host_id: [vm_ids]} -> {vm_id: host_id}
    flat = {}
    for hid, vm_ids in result["host_assignments"].items():
        for vid in vm_ids:
            flat[vid] = hid
    project.host_assignments = flat
    db.commit()
    logger.info(
        "Deploy %s: multi-host placement across %d hosts",
        project.id[:8],
        len(result["host_assignments"]),
    )


def _check_single_host_disk(project, result, db):
    """Check disk usage on single-host deployment target."""
    from app.services.troshkad_client import check_disk_usage

    host = db.query(Host).filter_by(id=result["host_id"]).first()
    if not host or not host.ip_address:
        return
    try:
        disk = check_disk_usage(host)
        if not disk:
            return
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


@router.post(
    "/{project_id}/deploy",
    responses={400: {}, 403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def deploy_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
    storage_pool_id: str | None = None,
    host_id: str | None = None,
    provider_id: str | None = None,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    if project.state != "draft":
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, not draft"
        )
    if not project.topology:
        raise HTTPException(status_code=400, detail="Project has no topology")

    from app.services.deploy_topology import (
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

    _validate_bmc_network(project.topology or {})
    _check_library_items_ready(project.topology, db)
    _validate_deploy_pool_and_host(db, user, storage_pool_id, host_id)

    if provider_id and not project.provider_id:
        project.provider_id = provider_id
        db.commit()

    result = place_project(
        db, project, storage_pool_id=storage_pool_id, host_id=host_id
    )
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])

    if result.get("multi_host"):
        _setup_multi_host_placement(project, result, db)
    else:
        _check_single_host_disk(project, result, db)
        # Persist VNI map for stop/start/destroy
        project.vni_map = result.get("vni_map")
        db.commit()

    # Deploy via job queue
    from app.core.redis import enqueue_job

    enqueue_job(deploy_project_async, project.id, project_id=project.id)

    if result.get("multi_host"):
        return {
            "status": "deploying",
            "multi_host": True,
            "host_count": len(result["host_assignments"]),
            "network_host_id": result["network_host_id"],
        }
    return {
        "status": "deploying",
        "host_id": result["host_id"],
        "host_ip": result["host_ip"],
        "requirements": result["requirements"],
    }


@router.post("/{project_id}/stop", responses={403: {}, 404: {}, 409: {}})
def stop_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    if project.state != "active":
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, not active"
        )

    project.state = "stopping"
    db.commit()
    notify_project(
        project_id, {"type": "project-state", "state": "stopping", "deploy_error": None}
    )

    from app.core.redis import enqueue_job

    enqueue_job(stop_project_async, project.id, project_id=project.id)

    return {"status": "stopping"}


def _force_stop_kubevirt_vms(host, project_id, vms, db):
    """Force-stop all VMs on a KubeVirt cluster."""
    from app.models.provider import Provider

    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return
    custom_api, _, _ = _get_k8s_clients_for_kubevirt(provider)
    namespace = _kubevirt_project_ns(provider, project_id)
    for vm in vms:
        kv_name = f"troshka-vm-{vm['id'][:8]}"
        try:
            custom_api.patch_namespaced_custom_object(
                group=_KUBEVIRT_API,
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=kv_name,
                body={"spec": {"running": False}},
            )
        except Exception as e:
            logger.warning("Failed to force-stop KubeVirt VM %s: %s", kv_name, e)


def _force_stop_troshkad_vms(host, project_id, vms):
    """Force-stop all VMs via troshkad."""
    if not host.ip_address:
        raise HTTPException(status_code=503, detail=_HOST_NOT_AVAILABLE)
    for vm in vms:
        dom = _domain_name(project_id, vm["id"])
        try:
            job_id = start_job(host, "/vms/force-off", {"domain_name": dom})
            wait_for_job(host, job_id, timeout=30, poll_interval=2)
        except TroshkadError:
            logger.warning("Failed to force-stop VM %s", dom)  # NOSONAR


@router.post("/{project_id}/force-stop", responses={403: {}, 404: {}, 503: {}})
def force_stop_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host:
        raise HTTPException(status_code=503, detail=_HOST_NOT_AVAILABLE)

    topo = project.deployed_topology or project.topology or {}
    vms = [n for n in topo.get("nodes", []) if n.get("type") == "vmNode"]

    if host.host_type == "kubevirt-cluster":
        _force_stop_kubevirt_vms(host, project_id, vms, db)
    else:
        _force_stop_troshkad_vms(host, project_id, vms)

    project.state = "stopped"
    db.commit()
    notify_project(
        project_id, {"type": "project-state", "state": "stopped", "deploy_error": None}
    )
    return {"status": "stopped"}


@router.post("/{project_id}/start", responses={403: {}, 404: {}, 409: {}})
def start_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    if project.state not in ("stopped", "error"):
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, not stopped"
        )

    project.state = "starting"
    project.deploy_started_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    notify_project(
        project_id, {"type": "project-state", "state": "starting", "deploy_error": None}
    )

    from app.core.redis import enqueue_job

    enqueue_job(start_project_async, project.id, project_id=project.id)

    return {"status": "starting"}


def _get_project_and_host(
    project_id: str, user: User, db: Session, check_disk: bool = False
):
    """Helper to load project + host with auth and state checks."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    if project.state not in ("active", "stopped"):
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, VMs not accessible"
        )
    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host:
        raise HTTPException(status_code=503, detail=_HOST_NOT_AVAILABLE)
    if host.host_type != "kubevirt-cluster" and (
        not host.private_key or not host.ip_address
    ):
        raise HTTPException(status_code=503, detail=_HOST_NOT_AVAILABLE)
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


def _set_redeploy_progress(dom: str, data: dict):
    from app.core.redis import set_progress

    set_progress(f"redeploy:{dom}", data)


def _get_redeploy_progress(dom: str) -> dict | None:
    from app.core.redis import get_progress

    return get_progress(f"redeploy:{dom}")


def _delete_redeploy_progress(dom: str):
    from app.core.redis import delete_progress

    delete_progress(f"redeploy:{dom}")


# Legacy compatibility — ws_pubsub imports this
_redeploy_progress: dict[str, dict] = {}


def _enforce_single_bastion_browser(topology: dict):
    """Ensure at most one VM has configureBastionBrowser enabled."""
    if not topology or not isinstance(topology, dict):
        return
    count = sum(
        1
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode"
        and n.get("data", {}).get("configureBastionBrowser")
    )
    if count > 1:
        raise HTTPException(
            status_code=400,
            detail="Only one VM can have 'Configure bastion browser' enabled per project",
        )


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
    from app.services.deploy_topology import _vm_domain_name

    return _vm_domain_name(project_id, vm_id)


def _build_destroy_context(project) -> dict:
    """Build the context dict needed by destroy_project_sync."""
    import copy

    return {
        "project_id": project.id,
        "host_id": project.host_id,
        "vni_map": copy.deepcopy(project.vni_map or {}),
        "topology": copy.deepcopy(project.deployed_topology or project.topology or {}),
        "dns_provider_id": project.dns_provider_id,
        "domain": project.domain,
    }


@router.get("/{project_id}/vm-states", responses={403: {}, 404: {}})
def get_all_vm_states(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    """Get actual running state of all VMs from libvirt."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    if not project.host_id:
        return {"states": {}}

    # Return cached states from the background poller when available
    # (avoids blocking troshkad calls on every browser poll)
    from app.services.ws_pubsub import get_cached_vm_states

    # Always return cached states from the background poller.
    # The poller is the single source of truth — no direct troshkad
    # calls from REST to avoid racing with the WS poller.
    cached = get_cached_vm_states(project_id)
    if cached:
        return cached
    return {"states": {}, "container_states": {}, "progress": {}}


@router.post(
    "/{project_id}/vms/{vm_id}/start",
    responses={403: {}, 404: {}, 409: {}, 500: {}, 503: {}, 507: {}},
)
def start_vm(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project, host = _get_project_and_host(project_id, user, db)

    # KubeVirt native: patch VM running state via K8s API
    if host.host_type == "kubevirt-cluster":
        from app.models.provider import Provider

        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            custom_api, _, _ = _get_k8s_clients_for_kubevirt(provider)
            kv_name = f"troshka-vm-{vm_id[:8]}"
            namespace = _kubevirt_project_ns(provider, project_id)
            try:
                custom_api.patch_namespaced_custom_object(
                    group=_KUBEVIRT_API,
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachines",
                    name=kv_name,
                    body={"spec": {"running": True}},
                )
            except Exception as e:
                raise HTTPException(500, f"Failed to start VM: {e}")
        if project.state == "stopped":
            project.state = "active"
            db.commit()
        return {"action": "start", "success": True}

    if project.state in ("stopped", "starting"):
        project.state = "starting"
        db.commit()
        p_id = project.id
        h_id = host.id
        target_vm_id = vm_id

        notify_project(
            project_id,
            {"type": "vm-state", "states": {vm_id: "starting"}, "progress": {}},
        )
        from app.core.redis import enqueue_job
        from app.workers.jobs import job_start_infra_then_vm

        enqueue_job(
            job_start_infra_then_vm,
            p_id,
            h_id,
            target_vm_id,
            project_id=p_id,
            host_id=h_id,
        )
        return {"action": "start", "success": True, "starting_project": True}

    # Start VM in background — re-cache images if needed, then virsh start
    notify_project(
        project_id, {"type": "vm-state", "states": {vm_id: "starting"}, "progress": {}}
    )

    p_id = project.id
    h_id = host.id

    from app.core.redis import enqueue_job
    from app.workers.jobs import job_cache_and_start_vm

    enqueue_job(
        job_cache_and_start_vm, p_id, h_id, vm_id, project_id=p_id, host_id=h_id
    )
    return {"action": "start", "success": True}


@router.post(
    "/{project_id}/vms/{vm_id}/stop",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def stop_vm(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
):
    _, host = _get_project_and_host(project_id, user, db)

    # KubeVirt native: patch VM running state via K8s API
    if host.host_type == "kubevirt-cluster":
        from app.models.provider import Provider

        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            custom_api, _, _ = _get_k8s_clients_for_kubevirt(provider)
            kv_name = f"troshka-vm-{vm_id[:8]}"
            namespace = _kubevirt_project_ns(provider, project_id)
            try:
                custom_api.patch_namespaced_custom_object(
                    group=_KUBEVIRT_API,
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachines",
                    name=kv_name,
                    body={"spec": {"running": False}},
                )
                notify_project(
                    project_id,
                    {"type": "vm-state", "states": {vm_id: "stopped"}, "progress": {}},
                )
                return {"action": "stop", "success": True}
            except Exception as e:
                logger.exception("Failed to stop KubeVirt VM %s: %s", kv_name, e)
                return {"action": "stop", "success": False}
        return {"action": "stop", "success": False}

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
        logger.exception("Failed to stop VM %s: %s", dom, e)
        return {"action": "stop", "success": False}


@router.get(
    "/{project_id}/vms/{vm_id}/status",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def get_vm_status(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
):
    _, host = _get_project_and_host(project_id, user, db)

    # KubeVirt native: read state from cached WS poller data
    if host.host_type == "kubevirt-cluster":
        from app.services.ws_pubsub import get_cached_vm_states

        cached = get_cached_vm_states(project_id)
        state = "unknown"
        if cached:
            state = cached.get("states", {}).get(vm_id, "unknown")
        return {"state": state, "boot_devs": []}

    dom = _domain_name(project_id, vm_id)
    vm_info = troshkad_get_vm_state(host, dom)
    return {"state": vm_info["state"], "boot_devs": vm_info.get("boot_devs", [])}


@router.post(
    "/{project_id}/vms/{vm_id}/forcestop",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def forcestop_vm(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
):
    _, host = _get_project_and_host(project_id, user, db)

    # KubeVirt native: stop VM + delete VMI for immediate shutdown
    if host.host_type == "kubevirt-cluster":
        from app.models.provider import Provider

        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            custom_api, _, _ = _get_k8s_clients_for_kubevirt(provider)
            kv_name = f"troshka-vm-{vm_id[:8]}"
            namespace = _kubevirt_project_ns(provider, project_id)
            try:
                custom_api.patch_namespaced_custom_object(
                    group=_KUBEVIRT_API,
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachines",
                    name=kv_name,
                    body={"spec": {"running": False}},
                )
                # Delete VMI for immediate effect (gracePeriodSeconds=0)
                try:
                    custom_api.delete_namespaced_custom_object(
                        group=_KUBEVIRT_API,
                        version="v1",
                        namespace=namespace,
                        plural="virtualmachineinstances",
                        name=kv_name,
                        grace_period_seconds=0,
                    )
                except Exception:
                    pass
                notify_project(
                    project_id,
                    {"type": "vm-state", "states": {vm_id: "stopped"}, "progress": {}},
                )
                return {"action": "forcestop", "success": True}
            except Exception as e:
                logger.exception(
                    "Failed to force-stop KubeVirt VM %s: %s", sanitize_log(kv_name), e
                )
                return {"action": "forcestop", "success": False}
        return {"action": "forcestop", "success": False}

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
        logger.exception("Failed to force-stop VM %s: %s", dom, e)
        return {"action": "forcestop", "success": False}


@router.post(
    "/{project_id}/vms/{vm_id}/restart",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def restart_vm(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
):
    _, host = _get_project_and_host(project_id, user, db)

    # KubeVirt native: delete VMI to trigger restart (VM CR stays running=true)
    if host.host_type == "kubevirt-cluster":
        from app.models.provider import Provider

        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            custom_api, _, _ = _get_k8s_clients_for_kubevirt(provider)
            kv_name = f"troshka-vm-{vm_id[:8]}"
            namespace = _kubevirt_project_ns(provider, project_id)
            try:
                custom_api.delete_namespaced_custom_object(
                    group=_KUBEVIRT_API,
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                    name=kv_name,
                )
                notify_project(
                    project_id,
                    {"type": "vm-state", "states": {vm_id: "running"}, "progress": {}},
                )
                return {"action": "restart", "success": True}
            except Exception as e:
                logger.exception("Failed to restart KubeVirt VM %s: %s", kv_name, e)
                return {"action": "restart", "success": False}
        return {"action": "restart", "success": False}

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
        logger.exception("Failed to restart VM %s: %s", dom, e)
        return {"action": "restart", "success": False}


@router.post(
    "/{project_id}/vms/{vm_id}/disks/{disk_node_id}/wipe",
    responses={403: {}, 404: {}, 409: {}, 501: {}, 503: {}, 507: {}},
)
def wipe_disk(
    project_id: str,
    vm_id: str,
    disk_node_id: str,
    user: CurrentUser,
    db: DbSession,
    restart: bool = False,
):
    project, host = _get_project_and_host(project_id, user, db)

    if host.host_type == "kubevirt-cluster":
        raise HTTPException(
            status_code=501, detail="Disk wipe not yet supported on KubeVirt"
        )

    topo = project.deployed_topology or project.topology or {}
    vm_node = next(
        (
            n
            for n in topo.get("nodes", [])
            if n["id"] == vm_id and n.get("type") == "vmNode"
        ),
        None,
    )
    if not vm_node:
        raise HTTPException(status_code=404, detail="VM not found in topology")

    disks = _find_vm_disks(vm_id, topo)
    disk = next((d for d in disks if d["node_id"] == disk_node_id), None)
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found on this VM")

    pool = _get_storage_pool_for_host(host, db)
    disk_path = _disk_path(project.id, vm_id, disk_node_id, disk["format"], pool=pool)

    dom = _domain_name(project_id, vm_id)
    vm_info = troshkad_get_vm_state(host, dom)
    was_running = vm_info["state"] == "running"

    if was_running:
        try:
            job_id = start_job(host, "/vms/stop", {"domain_name": dom})
            wait_for_job(host, job_id, timeout=120, poll_interval=2)
        except TroshkadError as e:
            raise HTTPException(
                status_code=409, detail=f"Failed to stop VM before wipe: {e}"
            )

    try:
        job_id = start_job(host, "/disks/wipe", {"path": disk_path})
        wait_for_job(host, job_id, timeout=60, poll_interval=2)
    except TroshkadError as e:
        logger.exception("Failed to wipe disk %s: %s", sanitize_log(disk_path), e)
        raise HTTPException(status_code=500, detail=f"Disk wipe failed: {e}")

    if restart or was_running:
        try:
            job_id = start_job(host, _VMS_START_PATH, {"domain_name": dom})
            wait_for_job(host, job_id, timeout=120, poll_interval=2)
        except TroshkadError as e:
            logger.warning("VM start after wipe failed: %s", e)

    return {"status": "wiped"}


@router.get(
    "/{project_id}/vms/{vm_id}/console",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def get_vm_console(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project, default_host = _get_project_and_host(project_id, user, db)

    # Determine which host this VM is on
    if project.host_assignments and vm_id in project.host_assignments:
        target_host_id = project.host_assignments[vm_id]
        host = db.query(Host).filter_by(id=target_host_id).first()
        if not host:
            raise HTTPException(status_code=404, detail="Host not found for VM")
    else:
        # Single-host project: use the host from _get_project_and_host
        host = default_host

    # KubeVirt native: VNC via proxy pod + OCP Route
    if host.host_type == "kubevirt-cluster":
        kv_vm_name = f"troshka-vm-{vm_id[:8]}"
        from app.models.provider import Provider
        from app.services.providers import get_provider_driver

        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if not provider:
            return {"error": "Provider not found"}
        driver = get_provider_driver(provider)
        cr_status = driver.get_project_status(provider, project_id)
        console_route = (
            cr_status.get("consoleRoute", "") if isinstance(cr_status, dict) else ""
        )
        if not console_route:
            return {"error": "Console not ready — VNC proxy route not yet available"}
        return {
            "ws_url": f"wss://{console_route}/{kv_vm_name}",
        }

    dom = _domain_name(project_id, vm_id)
    vnc_port = troshkad_get_vnc_port(host, dom)

    if not vnc_port:
        return {"error": "VNC not available"}

    if not host.console_domain or not host.agent_token:
        return {"error": "Console proxy not configured for this host"}

    from app.services.console_dns import sign_console_jwt

    jwt = sign_console_jwt(dom, host.id, host.agent_token)
    return {
        "ws_url": f"wss://{host.console_domain}/ws/{jwt}",
        "host_type": host.host_type,
    }


@router.get(
    "/{project_id}/vms/{vm_id}/ready",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def vm_ready(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
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


def _resolve_console_credentials(root_password, password, username, method_label):
    """Resolve console credentials. Returns (effective_user, effective_pass, error)."""
    effective_pass = root_password or password
    if not effective_pass:
        return None, None, f"{method_label}: no password available"
    effective_user = "root" if root_password else username
    return effective_user, effective_pass, None


def _try_kubevirt_method(
    m: str,
    provider,
    project_id: str,
    vm_id: str,
    vm_ip: str,
    username: str,
    password: str,
    root_password: str,
    command: str,
    timeout: int,
):
    """Try a single KubeVirt exec method. Returns (result, error_string | None)."""
    from app.services.providers.kubevirt import (
        kubevirt_exec_console,
        kubevirt_exec_guest_agent,
        kubevirt_exec_ssh,
        kubevirt_exec_vnc,
    )

    if m == "guest-agent":
        return (
            kubevirt_exec_guest_agent(provider, project_id, vm_id, command, timeout),
            None,
        )
    if m == "ssh":
        if not vm_ip or not password:
            return None, "ssh: no VM IP or credentials"
        return (
            kubevirt_exec_ssh(
                provider, project_id, vm_id, vm_ip, username, password, command, timeout
            ),
            None,
        )
    if m == "vnc":
        user_, pass_, err = _resolve_console_credentials(
            root_password, password, username, "vnc"
        )
        if err:
            return None, err
        return (
            kubevirt_exec_vnc(
                provider, project_id, vm_id, user_, pass_, command, timeout
            ),
            None,
        )
    if m in ("console", "serial"):
        user_, pass_, err = _resolve_console_credentials(
            root_password, password, username, "console"
        )
        if err:
            return None, err
        return (
            kubevirt_exec_console(
                provider, project_id, vm_id, user_, pass_, command, timeout
            ),
            None,
        )
    return None, f"{m}: unknown method"


def _exec_kubevirt(
    provider,
    project_id: str,
    vm_id: str,
    methods: list,
    vm_ip: str,
    username: str,
    password: str,
    root_password: str,
    command: str,
    timeout: int,
):
    """Dispatch exec to a KubeVirt-hosted VM. Returns result dict or raises HTTPException."""
    is_auto = len(methods) > 1
    errors: list[str] = []

    for m in methods:
        try:
            result, err = _try_kubevirt_method(
                m,
                provider,
                project_id,
                vm_id,
                vm_ip,
                username,
                password,
                root_password,
                command,
                timeout,
            )
            if result is not None:
                return result
            if err:
                errors.append(err)
        except Exception as e:
            errors.append(f"{m}: {e}")
            if not is_auto:
                raise HTTPException(status_code=503, detail=f"{m} exec failed: {e}")

    raise HTTPException(
        status_code=503,
        detail="All exec methods failed: " + "; ".join(errors),
    )


def _troshkad_exec_guest_agent(host, dom: str, command: str, timeout: int):
    """Execute via guest-agent on a troshkad host. Returns (result, error)."""
    job_id = start_job(
        host,
        "/vm/guest-exec",
        {"domain_name": dom, "command": command, "timeout": timeout},
    )
    job = wait_for_job(host, job_id, timeout=timeout + 30)
    if job["status"] == "completed":
        result = job.get("result", {})
        return {
            "output": result.get("output", ""),
            "error": result.get("error", ""),
            "exit_code": result.get("exit_code", 0),
            "method": "guest-agent",
        }, None
    return None, f"guest-agent: {job.get('result', {}).get('error', 'failed')}"


def _troshkad_exec_ssh(
    host,
    project_id: str,
    vm_ip: str,
    username: str,
    password: str,
    private_key: str,
    command: str,
    timeout: int,
):
    """Execute via SSH on a troshkad host. Returns (result, error)."""
    if not vm_ip or not (password or private_key):
        return None, "ssh: no VM IP or credentials"
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
        }, None
    return None, f"ssh: {job.get('result', {}).get('error', 'failed')}"


def _troshkad_exec_serial(
    host,
    dom: str,
    username: str,
    password: str,
    command: str,
    timeout: int,
):
    """Execute via serial console on a troshkad host. Returns (result, error)."""
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
            }, None
    return None, f"serial: {job.get('result', {}).get('error', 'failed')}"


def _troshkad_exec_console(
    host,
    dom: str,
    username: str,
    password: str,
    root_password: str,
    command: str,
    timeout: int,
    force_tty: bool,
    method_name: str,
):
    """Execute via VNC/text console on a troshkad host. Returns (result, error)."""
    console_pass = root_password or password
    if not console_pass:
        return None, "console: no password available"
    job_id = start_job(
        host,
        "/vm/console-exec",
        {
            "domain_name": dom,
            "username": "root" if root_password else username,
            "password": console_pass,
            "command": command,
            "timeout": timeout,
            "force_tty": method_name == "console-text" or force_tty,
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
            }, None
    return None, f"console: {job.get('result', {}).get('error', 'failed')}"


def _dispatch_troshkad_method(
    host,
    dom,
    m,
    project_id,
    vm_ip,
    username,
    password,
    private_key,
    root_password,
    command,
    timeout,
    force_tty,
):
    """Dispatch a single exec method on a troshkad host. Returns (result, error)."""
    if m == "guest-agent":
        return _troshkad_exec_guest_agent(host, dom, command, timeout)
    if m == "ssh":
        return _troshkad_exec_ssh(
            host, project_id, vm_ip, username, password, private_key, command, timeout
        )
    if m == "serial":
        return _troshkad_exec_serial(host, dom, username, password, command, timeout)
    if m in ("console", "console-text"):
        return _troshkad_exec_console(
            host, dom, username, password, root_password, command, timeout, force_tty, m
        )
    return None, f"{m}: unknown method"


def _exec_troshkad(
    host,
    project_id: str,
    vm_id: str,
    methods: list,
    method: str,
    vm_ip: str,
    username: str,
    password: str,
    private_key: str,
    root_password: str,
    command: str,
    timeout: int,
    force_tty: bool,
):
    """Dispatch exec to a troshkad-hosted VM. Returns result dict or raises HTTPException."""
    dom = _domain_name(project_id, vm_id)
    errors: list[str] = []

    for m in methods:
        try:
            result, err = _dispatch_troshkad_method(
                host,
                dom,
                m,
                project_id,
                vm_ip,
                username,
                password,
                private_key,
                root_password,
                command,
                timeout,
                force_tty,
            )

            if result is not None:
                return result
            if err:
                errors.append(err)

        except TroshkadError as e:
            errors.append(f"{m}: {e}")
            if method != "auto":
                raise HTTPException(status_code=503, detail=f"{m} exec failed: {e}")

    raise HTTPException(
        status_code=503,
        detail="All exec methods failed: " + "; ".join(errors),
    )


def _resolve_exec_params(body: dict, vm_node: dict | None) -> dict:
    """Extract and resolve exec parameters from request body and VM topology node."""
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
    root_password = ""
    if vm_node:
        root_password = vm_node.get("data", {}).get("ciRootPassword", "")

    if method == "auto":
        methods = ["guest-agent", "ssh", "console", "serial"]
        force_tty = False
    else:
        methods = [method]

    return {
        "username": username,
        "password": password,
        "timeout": timeout,
        "method": method,
        "force_tty": force_tty,
        "vm_ip": vm_ip,
        "private_key": private_key,
        "root_password": root_password,
        "methods": methods,
    }


@router.post(
    "/{project_id}/vms/{vm_id}/exec",
    responses={400: {}, 403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def vm_exec(
    project_id: str,
    vm_id: str,
    body: dict,
    user: CurrentUser,
    db: DbSession,
):
    """Execute a command on a VM.

    Body params:
        command: Shell command to execute (required)
        username: SSH/console user (default: cloud-user)
        password: VM password (auto-resolved from topology if omitted)
        timeout: Command timeout in seconds (default: 600, max: 3600)
        method: "auto" (tries guest-agent → ssh → console → serial),
                "guest-agent", "ssh", "serial", or "console"
    """
    project, host = _get_project_and_host(project_id, user, db)
    if project.state not in ("active", "stopped"):
        raise HTTPException(status_code=409, detail=_PROJECT_MUST_BE_ACTIVE)

    command = body.get("command", "")
    if not command:
        raise HTTPException(status_code=400, detail="Command is required")

    vm_node = next(
        (n for n in (project.topology or {}).get("nodes", []) if n["id"] == vm_id),
        None,
    )
    params = _resolve_exec_params(body, vm_node)

    if host.host_type == "kubevirt-cluster":
        from app.models.provider import Provider

        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if not provider:
            raise HTTPException(status_code=503, detail="Provider not found")

        kv_methods = params["methods"]
        if params["method"] == "auto":
            kv_methods = ["guest-agent", "ssh", "vnc", "console"]

        return _exec_kubevirt(
            provider,
            project_id,
            vm_id,
            kv_methods,
            params["vm_ip"],
            params["username"],
            params["password"],
            params["root_password"],
            command,
            params["timeout"],
        )

    return _exec_troshkad(
        host,
        project_id,
        vm_id,
        params["methods"],
        params["method"],
        params["vm_ip"],
        params["username"],
        params["password"],
        params["private_key"],
        params["root_password"],
        command,
        params["timeout"],
        params["force_tty"],
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


@router.put(
    "/{project_id}/vms/{vm_id}/files",
    responses={400: {}, 403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
async def vm_upload_file(
    project_id: str,
    vm_id: str,
    file: UploadFile,
    remote_path: Annotated[str, Query(description="Destination path on the VM")],
    user: CurrentUser,
    db: DbSession,
    mode: Annotated[str, Query(description="File permissions (octal)")] = "0644",
    username: Annotated[str, Query()] = "cloud-user",
    password: Annotated[str, Query()] = "",
    private_key: Annotated[str, Query()] = "",
):
    """Upload a file to a VM via SCP."""
    project, host = _get_project_and_host(project_id, user, db)
    if project.state not in ("active", "stopped"):
        raise HTTPException(status_code=409, detail=_PROJECT_MUST_BE_ACTIVE)

    _, vm_ip, topo_password = _resolve_vm_ssh_params(project, vm_id)
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


@router.get(
    "/{project_id}/vms/{vm_id}/files",
    responses={400: {}, 403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def vm_download_file(
    project_id: str,
    vm_id: str,
    remote_path: Annotated[str, Query(description="Path of the file on the VM")],
    user: CurrentUser,
    db: DbSession,
    username: Annotated[str, Query()] = "cloud-user",
    password: Annotated[str, Query()] = "",
):
    """Download a file from a VM via SCP."""
    project, host = _get_project_and_host(project_id, user, db)
    if project.state not in ("active", "stopped"):
        raise HTTPException(status_code=409, detail=_PROJECT_MUST_BE_ACTIVE)

    _, vm_ip, topo_password = _resolve_vm_ssh_params(project, vm_id)
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


@router.get(
    "/{project_id}/containers/{container_id}/logs",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def get_container_logs(
    project_id: str,
    container_id: str,
    user: CurrentUser,
    db: DbSession,
    tail: Annotated[
        int, Query(description="Number of lines to retrieve from the end")
    ] = 500,
):
    """Get logs from a container."""
    _, host = _get_project_and_host(project_id, user, db)
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
        logger.exception("Failed to get logs for container %s: %s", container_name, e)
        raise HTTPException(status_code=503, detail=str(e))


@router.post(
    "/{project_id}/containers/{container_id}/start",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def start_container(
    project_id: str,
    container_id: str,
    user: CurrentUser,
    db: DbSession,
):
    _, host = _get_project_and_host(project_id, user, db)
    container_name = f"troshka-{project_id[:8]}-{container_id[:8]}"
    try:
        job_id = start_job(
            host, "/containers/start", {"container_name": container_name}
        )
        wait_for_job(host, job_id, timeout=30)
        return {"status": "started", "container_name": container_name}
    except TroshkadError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post(
    "/{project_id}/containers/{container_id}/stop",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def stop_container(
    project_id: str,
    container_id: str,
    user: CurrentUser,
    db: DbSession,
):
    _, host = _get_project_and_host(project_id, user, db)
    container_name = f"troshka-{project_id[:8]}-{container_id[:8]}"
    try:
        job_id = start_job(
            host, "/containers/stop", {"container_name": container_name, "timeout": 10}
        )
        wait_for_job(host, job_id, timeout=30)
        return {"status": "stopped", "container_name": container_name}
    except TroshkadError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post(
    "/{project_id}/containers/{container_id}/restart",
    responses={403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def restart_container(
    project_id: str,
    container_id: str,
    user: CurrentUser,
    db: DbSession,
):
    _, host = _get_project_and_host(project_id, user, db)
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


def _allocate_vnis_for_new_networks(db, diff, vni_map):
    """Allocate VNIs for newly added networks. Modifies vni_map in place."""
    if not diff["added_networks"]:
        return
    from app.services.vxlan import VNI_MAX, VNI_MIN, _get_all_used_vnis

    used_vnis = _get_all_used_vnis(db) | set(vni_map.values())
    next_vni = VNI_MIN
    for net_node in diff["added_networks"]:
        data = net_node.get("data", {})
        if (
            data.get("subtype") == "network"
            and data.get("networkType") != "bmc"
            and net_node["id"] not in vni_map
        ):
            while next_vni in used_vnis:
                next_vni += 1
            if next_vni > VNI_MAX:
                raise HTTPException(status_code=507, detail="VNI pool exhausted")
            vni_map[net_node["id"]] = next_vni
            used_vnis.add(next_vni)
            next_vni += 1


@router.post(
    "/{project_id}/reconfigure",
    responses={400: {}, 403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def reconfigure_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
    body: dict | None = None,
):
    """Apply config changes (boot order, CPU, RAM) without destroying disks."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    if project.state not in ("active", "stopped"):
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, cannot reconfigure"
        )
    if not project.host_id:
        raise HTTPException(status_code=400, detail="Project has no active deployment")

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host:
        raise HTTPException(status_code=503, detail=_HOST_NOT_AVAILABLE)
    if host.host_type != "kubevirt-cluster" and (
        not host.private_key or not host.ip_address
    ):
        raise HTTPException(status_code=503, detail=_HOST_NOT_AVAILABLE)

    current = project.topology or {}
    _validate_bmc_network(current)

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
    _allocate_vnis_for_new_networks(db, diff, vni_map)
    project.vni_map = vni_map
    project.state = "reconfiguring"
    db.commit()

    restart_vm_ids = set((body or {}).get("restart_vm_ids", []))
    p_id = project.id
    h_id = host.id

    from app.core.redis import enqueue_job

    enqueue_job(
        _do_reconfigure_bg,
        p_id,
        h_id,
        list(restart_vm_ids),
        project_id=p_id,
        host_id=h_id,
    )
    return {"status": "reconfiguring"}


def _build_kubevirt_vm_spec(vm_id: str, vm: dict, current: dict) -> dict:
    """Build a TroshkaVM CR spec dict from topology data."""
    from app.services.deploy_topology import _find_vm_disks

    vm_disks = _find_vm_disks(vm_id, current)
    disk_specs = []
    for d in vm_disks:
        disk_spec = {
            "id": d.get("node_id", d.get("id", "")),
            "sizeGb": int(d.get("size", 20)),
            "bus": "virtio",
            "format": d.get("format", "qcow2"),
        }
        if d.get("source") == "pattern" and d.get("patternId"):
            disk_spec["patternImage"] = {
                "s3Path": d.get("resolvedS3Path", ""),
                "format": "qcow2",
                "central": d.get("centralSource", False),
            }
        elif d.get("source") == "library" and d.get("libraryItemId"):
            disk_spec["libraryImage"] = {
                "s3Path": d.get("resolvedS3Path", ""),
                "format": d.get("format", "qcow2"),
                "central": d.get("centralSource", False),
            }
        else:
            disk_spec["blank"] = True
        disk_specs.append(disk_spec)
    vm_data = {}
    for n in current.get("nodes", []):
        if n.get("id") == vm_id and n.get("type") == "vmNode":
            vm_data = n.get("data", {})
            break
    return {
        "vmId": vm_data.get("id", vm_id),
        "name": vm.get("name", "vm"),
        "cpus": vm.get("vcpus", 2),
        "memory": vm.get("ram_gb", 4) * 1024,
        "firmware": vm.get("firmware", "bios"),
        "machineType": "q35",
        "smbiosUuid": vm_data.get("smbiosUuid") or vm_data.get("domainUuid", ""),
        "os": vm.get("os", ""),
        "powerOnAtDeploy": vm_data.get("powerOnAtDeploy", True),
        "recertEnabled": vm_data.get("recertEnabled", False),
        "ocpMonitor": vm_data.get("ocpMonitor", False),
        "configureBastionBrowser": vm_data.get("configureBastionBrowser", False),
        "bmcEnabled": vm_data.get("bmcEnabled", False),
        "disks": disk_specs,
        "nics": vm_data.get("nics", []),
        "bootOrder": vm_data.get("bootDevices", []),
        "cloudInit": {
            "userData": vm_data.get("ciGeneratedUserData", "")
            or vm_data.get("ciUserData", ""),
            "networkConfig": vm_data.get("ciNetworkConfig", ""),
        },
    }


def _wait_kubevirt_vms_ready(custom_api, ns, p_id, proj, s, deadline_secs=300):
    """Poll TroshkaVM CRs until all are ready. Returns error string or None."""
    import time

    from app.services.deploy_service import (
        _delete_deploy_progress,
        _set_deploy_progress,
    )

    _set_deploy_progress(p_id, {"step": "reconfigure", "detail": "waiting for VMs"})
    deadline = time.time() + deadline_secs
    while time.time() < deadline:
        all_ready = True
        try:
            vms = custom_api.list_namespaced_custom_object(
                group=_TROSHKA_DOMAIN,
                version="v1alpha1",
                namespace=ns,
                plural="troshkavms",
            )
            for vm in dict(vms).get("items", []):  # type: ignore[call-overload]
                state = vm.get("status", {}).get("state", "")
                if state in ("Creating", "Reconfiguring", ""):
                    all_ready = False
                elif state == "Error":
                    msg = vm.get("status", {}).get("message", "")
                    proj.state = "error"
                    proj.deploy_error = (
                        f"VM {vm['spec'].get('name', '?')} failed: {msg}"
                    )
                    s.commit()
                    _delete_deploy_progress(p_id)
                    return "vm_error"
        except Exception:
            all_ready = False
        if all_ready:
            break
        time.sleep(5)
    return None


def _apply_kubevirt_vm_changes(
    custom_api,
    ns,
    p_id,
    diff,
    changed_vm_ids,
    current_vms,
    current,
):
    """Delete removed VMs and patch changed VMs on KubeVirt."""
    # Delete removed VMs
    for vm_id in diff.get("removed_vms", []):
        cr_name = f"vm-{vm_id[:8]}"
        try:
            custom_api.delete_namespaced_custom_object(
                group=_TROSHKA_DOMAIN,
                version="v1alpha1",
                namespace=ns,
                plural="troshkavms",
                name=cr_name,
            )
            logger.info("Reconfigure %s: deleted TroshkaVM %s", p_id[:8], cr_name)
        except Exception:
            pass

    # Patch changed VMs — update TroshkaVM CR spec, operator handles reconciliation
    for vm_id in changed_vm_ids:
        vm = current_vms.get(vm_id)
        if not vm:
            continue
        cr_name = f"vm-{vm_id[:8]}"
        vm_spec = _build_kubevirt_vm_spec(vm_id, vm, current)
        try:
            existing = custom_api.get_namespaced_custom_object(  # type: ignore[assignment]
                group=_TROSHKA_DOMAIN,
                version="v1alpha1",
                namespace=ns,
                plural="troshkavms",
                name=cr_name,
            )
            existing["spec"] = vm_spec  # type: ignore[index]
            custom_api.replace_namespaced_custom_object(
                group=_TROSHKA_DOMAIN,
                version="v1alpha1",
                namespace=ns,
                plural="troshkavms",
                name=cr_name,
                body=existing,
            )
            logger.info("Reconfigure %s: updated TroshkaVM %s", p_id[:8], cr_name)
        except Exception as e:
            logger.warning(
                "Reconfigure %s: failed to update %s: %s", p_id[:8], cr_name, e
            )


def _find_changed_kubevirt_vms(current: dict, deployed: dict) -> list[str]:
    """Find VM node IDs with any data change between current and deployed topologies."""
    cur_nodes = {
        n["id"]: n for n in current.get("nodes", []) if n.get("type") == "vmNode"
    }
    dep_nodes = {
        n["id"]: n for n in deployed.get("nodes", []) if n.get("type") == "vmNode"
    }
    return [
        nid
        for nid in cur_nodes
        if nid in dep_nodes and cur_nodes[nid].get("data") != dep_nodes[nid].get("data")
    ]


def _do_reconfigure_kubevirt(p_id: str, h_id: str, current: dict, deployed: dict):
    """Reconfigure a KubeVirt project by patching CRs."""
    import copy

    from app.core.database import SessionLocal
    from app.models.provider import Provider as ProviderModel
    from app.services.deploy_service import (
        _delete_deploy_progress,
        _set_deploy_progress,
    )
    from app.services.deploy_topology import diff_topologies
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns
    from app.services.ws_pubsub import notify_project

    s = SessionLocal()
    try:
        proj = s.query(Project).filter_by(id=p_id).first()
        h = s.query(Host).filter_by(id=h_id).first()
        if not proj or not h:
            return
        provider = (
            s.query(ProviderModel).filter_by(id=h.provider_id).first()
            if h.provider_id
            else None
        )
        if not provider:
            proj.state = "error"
            proj.deploy_error = "No provider found for host"
            s.commit()
            return

        diff = (
            diff_topologies(current, deployed)
            if deployed
            else {
                "added_vms": [],
                "removed_vms": [],
                "changed_vms": [],
                "has_changes": False,
            }
        )

        custom_api, _, _ = _get_k8s_clients(provider)
        ns = _project_ns(provider, p_id)

        _set_deploy_progress(
            p_id, {"step": "reconfigure", "detail": "applying changes"}
        )

        from app.services.deploy_topology import _extract_vms

        current_vms = {v["node_id"]: v for v in _extract_vms(current)}
        changed_vm_ids = _find_changed_kubevirt_vms(current, deployed)

        _apply_kubevirt_vm_changes(
            custom_api,
            ns,
            p_id,
            diff,
            changed_vm_ids,
            current_vms,
            current,
        )

        # Wait for all VMs to settle
        err = _wait_kubevirt_vms_ready(custom_api, ns, p_id, proj, s)
        if err:
            return

        # Sync EIPs (allocate new, release removed)
        errors: list[str] = []
        _sync_eips_for_reconfigure(s, proj, h, p_id, current, errors)
        if errors:
            logger.warning("Reconfigure %s: EIP errors: %s", p_id[:8], errors)

        from app.services.deploy_service import _patch_kubevirt_gateway_forwards

        _patch_kubevirt_gateway_forwards(provider, p_id, current)

        # Finalize
        _finalize_kubevirt_reconfigure(proj, s, p_id, current, copy, notify_project)
        _delete_deploy_progress(p_id)
    except Exception as e:
        logger.exception("Reconfigure %s: kubevirt error: %s", p_id[:8], e)
        try:
            proj = s.query(Project).filter_by(id=p_id).first()
            if proj:
                proj.state = "error"
                proj.deploy_error = str(e)[:500]
                s.commit()
        except Exception:
            pass
        _delete_deploy_progress(p_id)
    finally:
        s.close()


def _finalize_kubevirt_reconfigure(proj, s, p_id, current, copy, notify_project):
    """Commit the reconfigured topology and mark the project active."""
    clean_topo = copy.deepcopy(current)
    for node in clean_topo.get("nodes", []):
        ndata = node.get("data", {})
        ndata.pop("resolvedS3Path", None)
        ndata.pop("presignedUrl", None)
        ndata.pop("ciGeneratedUserData", None)
    proj.deployed_topology = clean_topo
    proj.topology = clean_topo
    proj.state = "active"
    proj.deploy_error = None
    s.commit()
    notify_project(p_id, {"type": "project-state", "state": "active"})
    logger.info("Reconfigure %s: kubevirt reconfigure complete", p_id[:8])


def _find_gateway_node(topology):
    """Find the NAT port-forward gateway node in a topology."""
    return next(
        (
            n
            for n in topology.get("nodes", [])
            if n.get("type") == "networkNode"
            and n.get("data", {}).get("subtype") == "gateway"
            and n.get("data", {}).get("gatewayMode") == "nat-portforward"
        ),
        None,
    )


def _sync_transit_ports(s, provider, h, p_id, gw_node):
    """Allocate transit ports and update EIP LB ports for non-EC2 providers."""
    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import allocate_transit_ports
    from app.services.providers import get_provider_driver

    driver = get_provider_driver(provider)
    pf_list = gw_node.get("data", {}).get("portForwards", [])
    eip_map = {}
    for eip_obj in s.query(ElasticIp).filter_by(project_id=p_id):
        eip_map[eip_obj.canvas_eip_id] = eip_obj

    for canvas_id, eip_obj in eip_map.items():
        pf_for_eip = [pf for pf in pf_list if pf.get("extIpId") == canvas_id]
        if not pf_for_eip:
            continue

        ns = None
        if provider.type == "kubevirt":
            from app.services.providers.kubevirt import _project_ns

            ns = _project_ns(provider, p_id)
            driver.update_eip_ports(
                provider,
                h,
                eip_obj.allocation_id,
                [
                    {
                        "port": int(pf.get("extPort", 443)),
                        "target_port": int(pf.get("extPort", 443)),
                        "name": f"pf-{i}",
                        "protocol": pf.get("proto", "tcp").upper(),
                    }
                    for i, pf in enumerate(pf_for_eip)
                ],
                namespace=ns,
            )
            logger.info(
                "Reconfigure %s: updated EIP LB ports (kubevirt direct)",
                p_id[:8],
            )
        else:
            eip_obj.port_map = None
            s.commit()
            port_map = allocate_transit_ports(s, eip_obj, h, pf_for_eip)
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


def _sync_eips_for_reconfigure(s, proj, h, p_id, current, errors):
    """Allocate/associate EIPs and sync security groups during reconfigure."""
    external_ips = current.get("externalIps", [])
    if not external_ips:
        return
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
        if not provider:
            return
        for ext_ip in external_ips:
            canvas_id = ext_ip.get("id", "")
            existing = (
                s.query(ElasticIp)
                .filter_by(project_id=p_id, canvas_eip_id=canvas_id)
                .first()
            )
            eip = existing or allocate_eip(s, provider, p_id, canvas_id, h)
            if eip.state != "associated":
                associate_eip(s, eip, h)
            ext_ip["ip"] = eip.public_ip
            ext_ip["_private_ip"] = eip.private_ip
        import copy
        import json

        from sqlalchemy import text

        new_topo = copy.deepcopy(current)
        s.execute(
            text("UPDATE projects SET topology = :topo WHERE id = :pid"),
            {"topo": json.dumps(new_topo), "pid": p_id},
        )
        s.commit()
        s.refresh(proj)

        gw_node = _find_gateway_node(current)
        if gw_node:
            desired_sg = [
                {
                    "project_id": p_id,
                    "ext_port": int(pf["extPort"]),
                    "protocol": "tcp",
                }
                for pf in gw_node.get("data", {}).get("portForwards", [])
                if pf.get("extPort")
            ]
            sync_security_group_rules(s, provider, desired_sg)

        if provider.type != "ec2" and gw_node:
            _sync_transit_ports(s, provider, h, p_id, gw_node)
    except Exception:
        logger.exception("EIP sync failed during reconfigure %s", p_id[:8])
        errors.append("EIP allocation/association failed — check server logs")


def _reconfigure_bmc(h, p_id, deployed, bmc_config, errors):
    """Teardown old BMC and set up new BMC during reconfigure."""
    from app.services.deploy_service import (
        _setup_bmc_via_troshkad,
        _teardown_bmc_via_troshkad,
    )

    deployed_had_bmc = any(
        n.get("type") == "networkNode" and n.get("data", {}).get("networkType") == "bmc"
        for n in deployed.get("nodes", [])
    )
    if deployed_had_bmc:
        try:
            _teardown_bmc_via_troshkad(h, p_id)
        except Exception:
            logger.warning("Reconfigure %s: BMC teardown failed (non-fatal)", p_id[:8])
    if bmc_config:
        try:
            bmc_result = _setup_bmc_via_troshkad(h, p_id, bmc_config)
            if bmc_result is not True:
                errors.append(f"BMC setup failed: {bmc_result}")
        except Exception:
            logger.warning("Reconfigure %s: BMC setup failed (non-fatal)", p_id[:8])
            errors.append("BMC setup failed — check server logs")


def _deploy_added_vms(h, p_id, s, current, vni_map, added_vms, errors):
    """Create and start newly added VMs during reconfigure."""
    from app.services.deploy_service import _set_deploy_progress
    from app.services.deploy_topology import _vm_domain_name

    _set_deploy_progress(p_id, {"step": "downloading", "detail": "0%"})

    def _progress(downloaded, total):
        pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
        _set_deploy_progress(p_id, {"step": "downloading", "detail": pct})

    cache_library_images(current, h, s, progress_callback=_progress)
    _create_seed_isos_via_troshkad(h, p_id, current)
    _set_deploy_progress(p_id, {"step": "creating", "detail": "VMs"})
    for vm_node in added_vms:
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
                job_id = start_job(h, _VMS_START_PATH, {"domain_name": vm_name})
                wait_for_job(h, job_id, timeout=60)
        except (TroshkadError, RuntimeError) as e:
            errors.append(f"Failed to add VM {vm_node['id'][:8]}: {e}")


def _broadcast_vm_states(h, p_id, current):
    """Query host for all VM states and broadcast via WebSocket."""
    from app.services.deploy_topology import _vm_domain_name

    try:
        from app.services.troshkad_client import get_all_vm_states

        batch = get_all_vm_states(h) or {}
        vm_states = {}
        for node in (current or {}).get("nodes", []):
            if node.get("type") != "vmNode":
                continue
            dom = _vm_domain_name(p_id, node["id"])
            raw = batch.get(dom, "unknown")
            if raw == "running":
                vm_states[node["id"]] = "running"
            elif raw == "shut_off":
                vm_states[node["id"]] = "stopped"
            else:
                vm_states[node["id"]] = raw
        notify_project(p_id, {"type": "vm-state", "states": vm_states, "progress": {}})
    except Exception:
        pass


def _get_deployed_disk_info(vm_node_id, deployed):
    """Extract library item IDs and sizes from deployed topology disks."""
    dep_disk_libs = {}
    dep_disk_sizes = {}
    dep_vm_node = next(
        (n for n in deployed.get("nodes", []) if n["id"] == vm_node_id),
        None,
    )
    if dep_vm_node:
        dep_disks = _find_vm_disks(vm_node_id, deployed)
        for dd in dep_disks:
            dep_disk_libs[dd["node_id"]] = dd.get("library_item_id")
            dep_disk_sizes[dd["node_id"]] = dd.get("size_gb", 0)
    return dep_disk_libs, dep_disk_sizes


def _resolve_disk_backing(d, pool):
    """Resolve the backing file path for a disk entry."""
    from app.services.deploy_topology import _image_cache_path

    if d.get("source") == "library" and d.get("library_item_id"):
        return _image_cache_path(d["library_item_id"], d["format"], pool=pool), True
    if d.get("source") == "pattern" and d.get("patternId"):
        backing = f"/var/lib/troshka/cache/patterns/{d['patternId']}/{d['patternDiskId']}.{d['format']}"
        return backing, False
    return None, False


def _classify_single_disk(d, p_id, vm_node_id, dep_disk_libs, dep_disk_sizes, pool):
    """Classify a single disk entry: detect image/size changes and resolve backing."""
    path = _disk_path(p_id, vm_node_id, d["node_id"], d["format"], pool=pool)

    old_lib = dep_disk_libs.get(d["node_id"])
    new_lib = d.get("library_item_id")
    image_changed = old_lib != new_lib and (old_lib or new_lib)
    old_size = dep_disk_sizes.get(d["node_id"], 0)
    size_grew = d["size_gb"] > old_size and old_size > 0
    is_new_disk = (
        d["node_id"] not in dep_disk_libs and d["node_id"] not in dep_disk_sizes
    )

    backing, is_library = _resolve_disk_backing(d, pool)
    info = {
        "path": path,
        "format": d["format"],
        "bus": d["bus"],
        "size_gb": d["size_gb"],
        "backing_file": backing,
        "image_changed": image_changed,
        "size_grew": size_grew,
        "is_new": is_new_disk,
        "is_library": is_library,
    }
    if d.get("rotation_rate") is not None:
        info["rotation_rate"] = d["rotation_rate"]
    return info


def _accumulate_disk_info(info, result):
    """Accumulate a single classified disk into the result dict."""
    disk_entry = {"path": info["path"], "format": info["format"], "bus": info["bus"]}
    if info.get("rotation_rate") is not None:
        disk_entry["rotation_rate"] = info["rotation_rate"]
    result["disk_list"].append(disk_entry)
    if info["image_changed"] or info["size_grew"] or info["is_new"]:
        result["any_disk_changed"] = True
    if info["image_changed"]:
        result["files_to_remove"].append(info["path"])
    if info["is_library"]:
        result["needs_library_download"] = True
    result["disks_to_create"].append(
        {
            "path": info["path"],
            "size_gb": info["size_gb"],
            "format": info["format"],
            "backing_file": info["backing_file"],
        }
    )
    if info["size_grew"] and not info["image_changed"]:
        result["disks_to_resize"].append(
            {"path": info["path"], "new_size_gb": info["size_gb"]}
        )


def _detect_disk_changes(p_id, vm_node_id, vm_disks, deployed, pool):
    """Build disk/cdrom lists and detect changes for a single VM."""
    result = {
        "disk_list": [],
        "cdrom_list": [],
        "any_disk_changed": False,
        "needs_library_download": False,
        "files_to_remove": [],
        "disks_to_create": [],
        "disks_to_resize": [],
    }
    if not vm_disks:
        return result

    from app.services.deploy_topology import _image_cache_path

    dep_disk_libs, dep_disk_sizes = _get_deployed_disk_info(vm_node_id, deployed)

    for d in vm_disks:
        if d["format"] == "iso":
            if d.get("library_item_id"):
                result["cdrom_list"].append(
                    _image_cache_path(d["library_item_id"], "iso", pool=pool)
                )
            continue

        info = _classify_single_disk(
            d, p_id, vm_node_id, dep_disk_libs, dep_disk_sizes, pool
        )
        _accumulate_disk_info(info, result)

    return result


def _apply_disk_changes(h, p_id, s, current, changes):
    """Remove old disk files, create new disks, and resize as needed."""
    from app.services.deploy_service import _set_deploy_progress

    if changes["needs_library_download"]:
        _set_deploy_progress(
            p_id,
            {"step": "checking images", "detail": ""},
        )
        cache_library_images(current, h, s)
    if changes["files_to_remove"]:
        try:
            job_id = start_job(
                h, _FILES_REMOVE_PATH, {"paths": changes["files_to_remove"]}
            )
            wait_for_job(h, job_id, timeout=30)
        except TroshkadError as e:
            logger.warning("Failed to remove old disk files: %s", e)
    for dc in changes["disks_to_create"]:
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
            logger.warning("Failed to create disk %s: %s", dc["path"], e)
    for dr in changes["disks_to_resize"]:
        try:
            job_id = start_job(h, "/disks/resize", dr)
            wait_for_job(h, job_id, timeout=60)
        except TroshkadError as e:
            logger.warning("Failed to resize disk %s: %s", dr["path"], e)


def _reconfigure_existing_vm(
    h, p_id, s, current, deployed, vm, vni_map, restart_vm_ids, pool, diff, errors
):
    """Handle reconfiguration of a single existing VM."""
    from app.services.deploy_service import _set_deploy_progress
    from app.services.deploy_topology import _resolve_boot_devs, _vm_domain_name

    dom = _vm_domain_name(p_id, vm["node_id"])
    vm_disks = _find_vm_disks(vm["node_id"], current)
    boot_devs = _resolve_boot_devs(vm, vm_disks, current)
    vm_networks = _find_vm_networks(vm["node_id"], current, vni_map, p_id)
    nics = [
        {"bridge": n["bridge"], "mac": n["mac"], "model": "virtio"} for n in vm_networks
    ] or None

    changes = _detect_disk_changes(p_id, vm["node_id"], vm_disks, deployed, pool)
    disk_list = changes["disk_list"]
    cdrom_list = changes["cdrom_list"]

    if vm.get("cloud_init"):
        cdrom_list.append(_seed_path(p_id, vm["node_id"], pool=pool))

    if changes["any_disk_changed"]:
        _apply_disk_changes(h, p_id, s, current, changes)

    current_cfg = troshkad_get_vm_config(h, dom)
    if not current_cfg:
        vm_node = next(
            (n for n in current.get("nodes", []) if n["id"] == vm["node_id"]),
            None,
        )
        if vm_node:
            diff["added_vms"].append(vm_node)
        return

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
        return

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
    _set_deploy_progress(p_id, {"step": "reconfiguring", "detail": vm["name"]})
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


def _finalize_reconfigure(s, proj, h, p_id, current, deployed, errors):
    """Commit final reconfigure state: BMC, topology, notifications."""
    import copy

    from app.services.deploy_service import _delete_deploy_progress
    from app.services.deploy_topology import _extract_bmc_config
    from app.services.placement import sync_host_capacity
    from app.services.ws_pubsub import notify_project

    sync_host_capacity(s, h)

    bmc_config = _extract_bmc_config(current, p_id)
    _reconfigure_bmc(h, p_id, deployed, bmc_config, errors)

    s.refresh(proj)
    final_topo = proj.topology or {}

    proj.state = "active"
    if not errors:
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
    _delete_deploy_progress(p_id)
    notify_project(
        p_id,
        {
            "type": "project-state",
            "state": "active",
            "deploy_error": proj.deploy_error,
        },
    )
    _broadcast_vm_states(h, p_id, current)
    logger.info(
        "Reconfigure %s complete%s",
        p_id[:8],
        f" with errors: {errors}" if errors else "",
    )


def _setup_reconfigure_networking(h, p_id, current, vni_map, s, proj):
    from app.services.deploy_service import (
        _delete_deploy_progress,
        _get_network_lock,
        _set_deploy_progress,
    )

    _set_deploy_progress(p_id, {"step": "networking", "detail": "configuring"})
    with _get_network_lock(h.id):
        net_result = _setup_networks_via_troshkad(h, current, vni_map, s, p_id)
    if net_result is not True:
        proj.state = "error"
        proj.deploy_error = f"Network setup failed: {net_result}"
        s.commit()
        _delete_deploy_progress(p_id)
        return False
    return True


def _cache_images_and_metadata(h, p_id, current, vni_map, s):
    from app.services.deploy_service import (
        _set_deploy_progress,
        _setup_metadata_via_troshkad,
    )

    _set_deploy_progress(p_id, {"step": "downloading", "detail": "0%"})

    def _reconfig_dl_progress(downloaded, total):
        pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
        _set_deploy_progress(p_id, {"step": "downloading", "detail": pct})

    cache_library_images(current, h, s, progress_callback=_reconfig_dl_progress)

    _set_deploy_progress(
        p_id,
        {"step": "cloud-init", "detail": "deploying metadata service"},
    )
    try:
        _setup_metadata_via_troshkad(h, p_id, current, vni_map)
        logger.info("Reconfigure %s: metadata service deployed", p_id[:8])
    except Exception:
        logger.exception(
            "Reconfigure %s: metadata service deployment failed (non-fatal)",
            p_id[:8],
        )
    _setup_pxe_via_troshkad(h, current, vni_map, p_id)


def _create_bmc_bridge_if_needed(h, p_id, current, bmc_config):
    _ = current
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
                "vms": [{"bmc_ip": vm["bmc_ip"]} for vm in bmc_config["vms"]],
            },
        )
        wait_for_job(h, bj, timeout=30)
    except TroshkadError:
        logger.warning(
            "Reconfigure %s: BMC bridge creation failed (non-fatal)", p_id[:8]
        )


def _remove_vms_from_reconfigure(h, p_id, diff, vm_dir_path):
    from app.services.deploy_topology import _vm_domain_name

    for node in diff["removed_vms"]:
        dom = _vm_domain_name(p_id, node["id"])
        troshkad_undefine_vm(h, dom)
        try:
            job_id = start_job(
                h,
                _FILES_REMOVE_PATH,
                {
                    "paths": [
                        f"{vm_dir_path}/{node['id'][:8]}-{suffix}" for suffix in ["*"]
                    ]
                },
            )
            wait_for_job(h, job_id, timeout=15)
        except TroshkadError:
            pass


def _get_storage_pool_for_host(h, s):
    if h.storage_pool_id:
        from app.models.storage_pool import StoragePool

        return s.query(StoragePool).filter_by(id=h.storage_pool_id).first()
    return None


def _reconfigure_process_vms(
    h, p_id, s, current, deployed, vni_map, restart_vm_ids, pool, diff, errors
):
    """Update existing VMs and deploy newly added VMs during reconfigure."""
    vms = _extract_vms(current)
    added_ids = {n["id"] for n in diff["added_vms"]}
    removed_ids = {n["id"] for n in diff["removed_vms"]}
    for vm in vms:
        if vm["node_id"] in added_ids or vm["node_id"] in removed_ids:
            continue
        _reconfigure_existing_vm(
            h,
            p_id,
            s,
            current,
            deployed,
            vm,
            vni_map,
            restart_vm_ids,
            pool,
            diff,
            errors,
        )

    if diff["added_vms"]:
        _deploy_added_vms(h, p_id, s, current, vni_map, diff["added_vms"], errors)


def _do_reconfigure_bg(p_id: str, h_id: str, restart_vm_ids: list | set):
    from app.core.database import SessionLocal
    from app.services.deploy_service import (
        _delete_deploy_progress,
    )
    from app.services.deploy_topology import _extract_bmc_config

    s = SessionLocal()
    try:
        proj = s.query(Project).filter_by(id=p_id).first()
        h = s.query(Host).filter_by(id=h_id).first()
        if not proj or not h:
            return

        current = proj.topology or {}
        deployed = proj.deployed_topology or {}

        if h.host_type == "kubevirt-cluster":
            s.close()
            _do_reconfigure_kubevirt(p_id, h_id, current, deployed)
            return

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
        _sync_eips_for_reconfigure(s, proj, h, p_id, current, errors)

        if not _setup_reconfigure_networking(h, p_id, current, vni_map, s, proj):
            return

        has_vm_changes = (
            diff.get("added_vms") or diff.get("removed_vms") or diff.get("changed_vms")
        )

        if has_vm_changes:
            _cache_images_and_metadata(h, p_id, current, vni_map, s)

        bmc_config = _extract_bmc_config(current, p_id)
        if bmc_config and has_vm_changes:
            _create_bmc_bridge_if_needed(h, p_id, current, bmc_config)

        _pool = _get_storage_pool_for_host(h, s)
        vm_dir_path = _vm_dir(p_id, pool=_pool)

        _remove_vms_from_reconfigure(h, p_id, diff, vm_dir_path)

        _reconfigure_process_vms(
            h,
            p_id,
            s,
            current,
            deployed,
            vni_map,
            restart_vm_ids,
            _pool,
            diff,
            errors,
        )

        _finalize_reconfigure(s, proj, h, p_id, current, deployed, errors)
    except Exception:
        logger.exception("Reconfigure %s failed", p_id[:8])
        proj = s.query(Project).filter_by(id=p_id).first()
        if proj:
            proj.state = "error"
            s.commit()
        _delete_deploy_progress(p_id)
    finally:
        s.close()


@router.post(
    "/{project_id}/vms/{vm_id}/redeploy",
    responses={400: {}, 403: {}, 404: {}, 409: {}, 503: {}, 507: {}},
)
def redeploy_vm(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
):
    """Destroy and recreate a single VM in a background thread."""
    project, host = _get_project_and_host(project_id, user, db, check_disk=True)
    _check_library_items_ready(project.topology or {}, db)

    p_id = project.id
    host_id = host.id
    target_vm_id = vm_id

    from app.core.redis import enqueue_job

    enqueue_job(
        _do_redeploy_bg, p_id, host_id, target_vm_id, project_id=p_id, host_id=host_id
    )
    return {"status": "redeploying"}


def _cleanup_old_vm_files(h, p_id, target_vm_id, topology):
    """Remove old disk and seed files for a VM being redeployed."""
    vm_disks_to_remove = _find_vm_disks(target_vm_id, topology or {})
    paths_to_remove = []
    for d in vm_disks_to_remove:
        if d["format"] != "iso":
            paths_to_remove.append(
                _disk_path(p_id, target_vm_id, d["node_id"], d["format"])
            )
    paths_to_remove.append(_seed_path(p_id, target_vm_id))
    try:
        job_id = start_job(h, _FILES_REMOVE_PATH, {"paths": paths_to_remove})
        wait_for_job(h, job_id, timeout=15)
    except TroshkadError as e:
        from app.services.deploy_topology import _vm_domain_name

        logger.warning(
            "Redeploy %s: failed to remove old files: %s",
            _vm_domain_name(p_id, target_vm_id),
            e,
        )


def _build_redeploy_vm_data(vm_node):
    """Build vm_data dict from a topology vm_node for redeploy."""
    vdata = vm_node.get("data", {})
    return {
        "node_id": vm_node["id"],
        "name": vdata.get("name", "vm"),
        "vcpus": vdata.get("vcpus", 2),
        "ram_gb": vdata.get("ram", 4),
        "cloud_init": vdata.get("cloudInit", False),
        "boot_devices": vdata.get("bootDevices"),
        "firmware": vdata.get("firmware", "bios"),
        "secure_boot": vdata.get("secureBoot", False),
    }


def _find_vm_node_in_topology(topology, target_vm_id):
    return next(
        (
            n
            for n in (topology or {}).get("nodes", [])
            if n["id"] == target_vm_id and n.get("type") == "vmNode"
        ),
        None,
    )


def _build_connected_topology(topology, target_vm_id):
    edges = (topology or {}).get("edges", [])
    vm_connected_ids = set()
    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src == target_vm_id:
            vm_connected_ids.add(tgt)
        elif tgt == target_vm_id:
            vm_connected_ids.add(src)
    return {
        "nodes": [
            n for n in (topology or {}).get("nodes", []) if n["id"] in vm_connected_ids
        ]
    }


def _cache_redeploy_images(h, s, vm_topo, dom):
    _redeploy_progress[dom] = {"step": "downloading", "detail": "0%"}

    def _progress(downloaded, total):
        pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
        _redeploy_progress[dom] = {"step": "downloading", "detail": pct}

    cache_library_images(vm_topo, h, s, progress_callback=_progress)


def _create_redeploy_vm(h, p_id, vm_node, topology, vni_map, pool, target_vm_id, dom):
    vm_only_topo = {"nodes": [vm_node], "edges": []}
    _redeploy_progress[dom] = {"step": "creating", "detail": "cloud-init seed ISO"}
    _create_seed_isos_via_troshkad(h, p_id, vm_only_topo, pool)

    _redeploy_progress[dom] = {"step": "creating", "detail": "VM definition"}
    vm_data = _build_redeploy_vm_data(vm_node)
    disk_cache = "none" if pool and pool.mode.startswith("shared") else None
    vm_disks = _find_vm_disks(target_vm_id, topology or {})
    _create_vm_disks_via_troshkad(h, p_id, vm_data, vm_disks, pool)
    _create_vm_via_troshkad(h, p_id, vm_data, topology or {}, vni_map, pool, disk_cache)


def _start_vm_if_needed(h, dom, was_running, vm_node):
    vdata = vm_node.get("data", {})
    should_start = was_running or vdata.get("powerOnAtDeploy", True)
    if should_start:
        try:
            job_id = start_job(h, _VMS_START_PATH, {"domain_name": dom})
            wait_for_job(h, job_id, timeout=60)
        except TroshkadError as e:
            logger.warning("Failed to start VM %s after redeploy: %s", dom, e)


def _do_redeploy_bg(p_id: str, host_id: str, target_vm_id: str):
    from app.core.database import SessionLocal
    from app.services.deploy_service import _get_host_pool
    from app.services.deploy_topology import _vm_domain_name

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
        _cleanup_old_vm_files(h, p_id, target_vm_id, topology)

        vm_node = _find_vm_node_in_topology(topology, target_vm_id)
        if not vm_node:
            logger.warning("Redeploy %s: node not found in topology", target_vm_id[:8])
            _redeploy_progress.pop(dom, None)
            return

        vm_topo = _build_connected_topology(topology, target_vm_id)
        _cache_redeploy_images(h, s, vm_topo, dom)
        _setup_pxe_via_troshkad(h, topology, vni_map, p_id)

        pool = _get_host_pool(h, s)
        _create_redeploy_vm(
            h, p_id, vm_node, topology, vni_map, pool, target_vm_id, dom
        )
        _start_vm_if_needed(h, dom, was_running, vm_node)

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


@router.post("/{project_id}/vms/{vm_id}/cancel-redeploy", responses={403: {}, 404: {}})
def cancel_redeploy(
    project_id: str,
    vm_id: str,
    user: CurrentUser,
    db: DbSession,
):
    """Cancel a stuck redeploy by clearing the progress tracker."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    dom = _domain_name(project_id, vm_id)
    _redeploy_progress.pop(dom, None)
    return {"status": "cancelled"}


@router.post(
    "/{project_id}/redeploy", responses={400: {}, 403: {}, 404: {}, 409: {}, 503: {}}
)
def redeploy_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    """Destroy existing infrastructure and redeploy with current topology."""
    project = db.query(Project).filter_by(id=project_id).with_for_update().first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)
    if project.state not in ("active", "stopped", "error"):
        raise HTTPException(
            status_code=409, detail=f"Project is {project.state}, cannot redeploy"
        )

    _check_library_items_ready(project.topology or {}, db)

    if not project.topology:
        raise HTTPException(status_code=400, detail="Project has no topology")

    reqs = calculate_project_requirements(project.topology)
    if reqs["vm_count"] == 0:
        raise HTTPException(status_code=400, detail="Project has no VMs")

    # Capture destroy context before resetting state
    destroy_ctx = None
    old_host_id = project.host_id
    if project.host_id:
        old_host = db.query(Host).filter_by(id=old_host_id).first()
        if not old_host or not old_host.ip_address:
            raise HTTPException(
                status_code=503,
                detail="Host not reachable — cannot destroy existing VMs. Stop the project first or wait for the host to come online.",
            )
        destroy_ctx = _build_destroy_context(project)

    # Cancel any in-flight deploy thread for this project
    from app.services.deploy_service import _mark_deploy_cancelled

    _mark_deploy_cancelled(project.id)

    # Set state to deploying and return immediately
    project.state = "deploying"
    project.host_id = old_host_id
    project.vni_map = None
    project.deploy_error = None
    project.ocp_status = None
    project.ocp_install_elapsed = None
    project.deploy_started_at = datetime.datetime.now(datetime.UTC)
    db.commit()

    from app.core.redis import enqueue_job
    from app.workers.jobs import job_redeploy_bg

    enqueue_job(
        job_redeploy_bg,
        project.id,
        destroy_ctx,
        old_host_id,
        project_id=project.id,
        host_id=old_host_id,
    )

    return {"status": "deploying"}


@router.post("/{project_id}/undeploy", responses={403: {}, 404: {}})
def undeploy_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    """Destroy all infrastructure and reset project to draft."""
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    if project.host_id:
        destroy_project_sync(
            {
                "project_id": project.id,
                "host_id": project.host_id,
                "vni_map": project.vni_map or {},
                "topology": project.deployed_topology or project.topology or {},
                "dns_provider_id": project.dns_provider_id,
                "domain": project.domain,
            },
            delete_record=False,
        )

    project.state = "draft"
    project.host_id = None
    project.vni_map = None
    project.deploy_error = None
    db.commit()

    return {"status": "draft"}


@router.delete("/{project_id}", responses={403: {}, 404: {}})
def delete_project(
    project_id: str,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    # Release EIPs
    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import release_eip

    project_eips = db.query(ElasticIp).filter_by(project_id=project_id).all()
    for eip in project_eips:
        try:
            release_eip(db, eip)
        except Exception:
            logger.warning("Failed to release EIP %s on delete", eip.public_ip)

    if project.host_id and project.state not in ("draft",):
        import copy

        project.state = "deleting"
        project.deploy_error = None
        db.commit()
        notify_project(project_id, {"type": "project-state", "state": "deleting"})

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
        from app.core.redis import enqueue_job

        enqueue_job(destroy_project_sync, destroy_ctx, project_id=project.id)
        return {"status": "deleting", "id": project_id}

    db.delete(project)
    db.commit()
    notify_project(project_id, {"type": "project-deleted"})


class ImportVMRequest(PydanticBaseModel):
    snapshot_id: str
    position_x: float = 100.0
    position_y: float = 100.0


def _create_snapshot_disk_nodes(
    disks, item, vm_id, position_x, position_y, dc_list, unique_name_fn, topology
):
    """Create storage nodes and edges for snapshot disks. Returns boot_devices list."""
    boot_devices = []
    for idx, disk_info in enumerate(disks):
        disk_id = str(uuid_mod.uuid4())
        disk_name = unique_name_fn(disk_info.get("name", "disk"))
        disk_node = {
            "id": disk_id,
            "type": "storageNode",
            "position": {"x": position_x - 250, "y": position_y + idx * 150},
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
    return boot_devices


def _wire_snapshot_network_edges(networks_info, nic_list, vm_id, topology):
    """Create edges connecting VM NICs to matching canvas networks."""
    canvas_networks = {
        n.get("data", {}).get("name", ""): n
        for n in topology["nodes"]
        if n.get("type") == "networkNode"
    }
    remaining_nics = list(nic_list)
    for net_info in networks_info:
        net_name = net_info.get("name", "")
        matching_net = canvas_networks.get(net_name)
        if not matching_net or not remaining_nics:
            continue
        nic = remaining_nics.pop(0)
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


@router.post(
    "/{project_id}/import-vm",
    response_model=ProjectResponse,
    responses={403: {}, 404: {}},
)
def import_vm_from_snapshot(
    project_id: str,
    body: ImportVMRequest,
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

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

    boot_devices = _create_snapshot_disk_nodes(
        disks,
        item,
        vm_id,
        body.position_x,
        body.position_y,
        dc_list,
        _unique_name,
        topology,
    )
    if boot_devices:
        vm_data["bootDevices"] = boot_devices

    _wire_snapshot_network_edges(
        vm_config.get("networks", []),
        vm_data["nics"],
        vm_id,
        topology,
    )

    project.topology = topology
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "topology")
    db.commit()
    db.refresh(project)
    return project


class MigrateRequest(PydanticBaseModel):
    target_host_id: str


@router.post("/{project_id}/migrate", responses={400: {}, 404: {}})
def migrate_project_endpoint(
    project_id: str,
    body: MigrateRequest,
    user: Annotated[User, Depends(require_role("admin"))],
    db: DbSession,
):
    from app.services.migration_service import migrate_project, validate_migration

    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, _PROJECT_NOT_FOUND)

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
