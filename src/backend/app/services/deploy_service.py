"""
Deploy service — creates VMs and networks on hosts via troshkad.

Translates canvas topology into libvirt VMs and VXLAN networks,
then sends structured commands to the troshkad agent on the host.
"""

import copy
import datetime
import ipaddress
import logging
import os
import threading
import time as _time

import boto3  # noqa: F401 — eager import to prevent thread race
import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401

from app.core.redis import (
    RedisSemaphore,
    add_to_set,
    clear_cancelled,
    delete_progress,
    get_lock,
    get_progress,
    is_in_set,
    remove_from_set,
    set_progress,
)  # noqa: F401
from app.core.redis import (
    is_cancelled as _redis_is_cancelled,
)
from app.core.redis import (
    mark_cancelled as _redis_mark_cancelled,
)
from app.models.host import Host
from app.models.pattern import Pattern
from app.services.troshkad_client import (
    TroshkadError,
    start_job,
    wait_for_job,
)
from app.services.ws_pubsub import notify_project

logger = logging.getLogger(__name__)

_deploy_semaphore = RedisSemaphore("deploy", limit=100, ttl=7200)

# Ordered deploy steps — used for checkpoint-based resume
DEPLOY_STEPS = [
    "eips",
    "networks",
    "seeds",
    "images",
    "container_pull",
    "disks",
    "vms",
    "containers",
    "starting",
    "dns",
    "done",
]

_HEALTH_MONITORS_SET = "deploy:health_monitors"

# Duplicated string constants (SonarQube S1192)
_MSG_WAITING_API = "waiting for API server"
_CMD_GET_NODES = "oc get nodes --no-headers 2>/dev/null"
_MSG_WAITING_CONSOLE = "waiting for OpenShift console"
_MSG_CA_CERT = "CA cert"
_MSG_BROWSER_CREDS = "browser credentials"


def _set_deploy_progress(project_id: str, data: dict):
    set_progress(f"deploy:{project_id}", data)


def _get_deploy_progress_data(project_id: str) -> dict | None:
    return get_progress(f"deploy:{project_id}")


def _delete_deploy_progress(project_id: str):
    delete_progress(f"deploy:{project_id}")


def _mark_deploy_cancelled(project_id: str):
    _redis_mark_cancelled(project_id)


def _is_deploy_cancelled(project_id: str) -> bool:
    return _redis_is_cancelled(project_id)


def _clear_deploy_cancelled(project_id: str):
    clear_cancelled(project_id)


def validate_topology_names(topology: dict) -> list[str]:
    """Check for duplicate node names within a topology. Returns list of errors."""
    errors = []
    seen: dict[str, dict[str, str]] = {"vm": {}, "network": {}, "storage": {}}
    type_labels = {"vm": "VM", "network": "Network", "storage": "Disk"}
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        name = data.get("name") or data.get("label", "")
        if not name:
            continue
        if node.get("type") == "vmNode":
            bucket = "vm"
        elif node.get("type") == "networkNode":
            bucket = "network"
        elif node.get("type") == "storageNode":
            bucket = "storage"
        else:
            continue
        if name in seen[bucket]:
            errors.append(f"Duplicate {type_labels[bucket]} name: '{name}'")
        else:
            seen[bucket][name] = node["id"]
    return errors


def validate_topology_ips(topology: dict) -> list[str]:
    """Check for duplicate IP addresses on the same network. Returns list of errors."""
    errors = []
    nodes_by_id: dict[str, dict] = {n["id"]: n for n in topology.get("nodes", [])}

    nic_to_network: dict[str, str] = {}
    for edge in topology.get("edges", []):
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        for handle_key, net_id, vm_id in [
            ("targetHandle", src, tgt),
            ("sourceHandle", tgt, src),
        ]:
            handle = edge.get(handle_key, "")
            if (
                nodes_by_id.get(net_id, {}).get("type") == "networkNode"
                and nodes_by_id.get(vm_id, {}).get("type")
                in ("vmNode", "containerNode")
                and "nic-" in handle
            ):
                raw = handle.replace("-top", "").replace("-bottom", "")
                if raw.startswith("nic-"):
                    nic_id = raw[4:]  # strip handle "nic-" wrapper
                    nic_to_network[nic_id] = net_id

    per_network: dict[str, dict[str, str]] = {}
    for node in topology.get("nodes", []):
        if node.get("type") not in ("vmNode", "containerNode"):
            continue
        vm_name = node.get("data", {}).get("name", "?")
        for nic in node.get("data", {}).get("nics", []):
            ip = nic.get("ip", "")
            if not ip:
                continue
            net_id = nic_to_network.get(nic["id"], "unconnected")
            net_name = (
                nodes_by_id.get(net_id, {}).get("data", {}).get("name", "unconnected")
            )
            if net_id not in per_network:
                per_network[net_id] = {}
            if ip in per_network[net_id]:
                other_vm = per_network[net_id][ip]
                errors.append(
                    f"Duplicate IP {ip} on network '{net_name}': "
                    f"used by both '{other_vm}' and '{vm_name}'"
                )
            else:
                per_network[net_id][ip] = vm_name
    return errors


def validate_topology_passwords(topology: dict) -> list[str]:
    """Check that required passwords are set. Returns list of errors."""
    errors = []
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") == "networkNode" and data.get("networkType") == "bmc":
            if not data.get("bmcPassword"):
                errors.append(
                    f"BMC network '{data.get('name', '?')}' has no password set"
                )
    return errors


def _update_deploy_progress(
    project_id: str, step: str, detail: str = "", items: list | None = None
):
    progress: dict = {"step": step, "detail": detail}
    if items is not None:
        progress["items"] = items
    _set_deploy_progress(project_id, progress)
    notify_project(project_id, {"type": "deploy-progress", "progress": progress})
    try:
        from app.core.database import SessionLocal as _DP_SL
        from app.models.project import Project as _DP_Proj

        _ds = _DP_SL()
        _dp = _ds.get(_DP_Proj, project_id)
        if _dp:
            _dp.deploy_progress = progress
            _ds.commit()
        _ds.close()
    except Exception:
        pass


def get_deploy_progress(project_id: str) -> dict | None:
    """Get deploy progress — Redis first, fall back to DB."""
    cached = _get_deploy_progress_data(project_id)
    if cached:
        return cached
    from app.core.database import SessionLocal as _SL
    from app.models.project import Project

    db = _SL()
    try:
        project = db.query(Project).filter_by(id=project_id).first()
        if project and project.deploy_progress:
            return project.deploy_progress
    finally:
        db.close()
    return None


def _checkpoint(session, project_id: str, step: str):
    """Persist deploy step to DB so deploy can resume after restart."""
    from app.models.project import Project

    project = session.query(Project).filter_by(id=project_id).first()
    if project:
        project.deploy_step = step
        progress = _get_deploy_progress_data(project_id)
        if progress:
            project.deploy_progress = progress
        session.commit()


def _should_skip(resume_from: str | None, step: str) -> bool:
    """Return True if this step was already completed before the restart."""
    if not resume_from:
        return False
    try:
        return DEPLOY_STEPS.index(step) < DEPLOY_STEPS.index(resume_from)
    except ValueError:
        return False


# Per-host locks for nftables-touching network setup (concurrent deploys on
# different hosts can proceed in parallel) — distributed via Redis


def _get_network_lock(host_id: str):
    return get_lock(f"network:{host_id}", timeout=120)


# ── Shared storage pool helpers ──


def _get_host_pool(host, db_session):
    """Get the storage pool for a host, if any."""
    if not host.storage_pool_id:
        return None
    from app.models.storage_pool import StoragePool

    return db_session.get(StoragePool, host.storage_pool_id)


def _check_shared_cache(db_session, pool, item_id, item_type):
    """Check if an item is cached on shared storage. Returns (status, entry) or (None, None)."""
    if not pool:
        return None, None
    from app.models.storage_pool import SharedCacheEntry

    entry = (
        db_session.query(SharedCacheEntry)
        .filter(
            SharedCacheEntry.storage_pool_id == pool.id,
            SharedCacheEntry.item_id == item_id,
            SharedCacheEntry.item_type == item_type,
        )
        .first()
    )
    if entry:
        return entry.status, entry
    return None, None


def _create_shared_cache_entry(db_session, pool, item_id, item_type, file_path):
    """Create a SharedCacheEntry with status='downloading'."""
    from app.models.storage_pool import SharedCacheEntry

    entry = SharedCacheEntry(
        storage_pool_id=pool.id,
        item_type=item_type,
        item_id=item_id,
        status="downloading",
        file_path=file_path,
    )
    db_session.add(entry)
    db_session.commit()
    return entry


def _mark_shared_cache_ready(db_session, pool_id, item_id, item_type, size_bytes=None):
    """Mark a shared cache entry as ready."""
    from app.models.storage_pool import SharedCacheEntry

    entry = (
        db_session.query(SharedCacheEntry)
        .filter(
            SharedCacheEntry.storage_pool_id == pool_id,
            SharedCacheEntry.item_id == item_id,
            SharedCacheEntry.item_type == item_type,
        )
        .first()
    )
    if entry:
        entry.status = "ready"
        if size_bytes:
            entry.size_bytes = size_bytes
        db_session.commit()


def _mark_shared_cache_error(db_session, pool_id, item_id, item_type):
    """Mark a shared cache entry as error so other deploys don't wait on it."""
    from app.models.storage_pool import SharedCacheEntry

    entry = (
        db_session.query(SharedCacheEntry)
        .filter(
            SharedCacheEntry.storage_pool_id == pool_id,
            SharedCacheEntry.item_id == item_id,
            SharedCacheEntry.item_type == item_type,
        )
        .first()
    )
    if entry and entry.status == "downloading":
        db_session.delete(entry)
        db_session.commit()


def _wait_for_shared_cache(db_session, pool_id, item_id, item_type, timeout=600):
    """Wait for another download to complete. Returns True if ready."""
    import time as _t

    from app.models.storage_pool import SharedCacheEntry

    deadline = _t.time() + timeout
    while _t.time() < deadline:
        db_session.expire_all()
        entry = (
            db_session.query(SharedCacheEntry)
            .filter(
                SharedCacheEntry.storage_pool_id == pool_id,
                SharedCacheEntry.item_id == item_id,
                SharedCacheEntry.item_type == item_type,
            )
            .first()
        )
        if entry and entry.status == "ready":
            return True
        if entry and entry.status == "error":
            return False
        _t.sleep(5)
    return False


# ── Topology parsing ──


def _extract_vms(topology: dict) -> list[dict]:
    """Extract VM nodes with their properties."""
    vms = []
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        vms.append(
            {
                "node_id": node["id"],
                "name": data.get("name", "vm"),
                "vcpus": data.get("vcpus", 2),
                "ram_gb": data.get("ram", 4),
                "os": data.get("os", ""),
                "nics": data.get("nics", []),
                "disk_controllers": data.get("diskControllers", []),
                "boot_devices": data.get("bootDevices", ["hd"]),
                "cloud_init": data.get("cloudInit", False),
                "firmware": data.get("firmware", "bios"),
                "secure_boot": data.get("secureBoot", False),
                "video_model": data.get("videoModel", "virtio"),
                "input_model": data.get("inputModel", "virtio"),
                "uuid": data.get("uuid"),
                "recertEnabled": data.get("recertEnabled", False),
                "ocpMonitor": data.get("ocpMonitor", False),
                "configureBastionBrowser": data.get("configureBastionBrowser", False),
            }
        )
    return vms


def _extract_containers(topology: dict) -> list[dict]:
    """Extract container nodes with their properties."""
    containers = []
    for node in topology.get("nodes", []):
        if node.get("type") != "containerNode":
            continue
        data = node.get("data", {})
        containers.append(
            {
                "node_id": node["id"],
                "name": data.get("name", "container"),
                "image": data.get("image", ""),
                "registry_credential_id": data.get("registryCredentialId"),
                "registry_credential_name": data.get("registryCredentialName"),
                "cpus": data.get("cpus", 1),
                "memory_mb": data.get("memory", 512),
                "nics": data.get("nics", []),
                "env_vars": data.get("envVars", []),
                "ports": data.get("ports", []),
                "command": data.get("command"),
                "restart_policy": data.get("restartPolicy", "always"),
                "privileged": data.get("privileged", False),
                "mounts": data.get("mounts", []),
                "is_pod": data.get("isPod", False),
                "init_containers": data.get("initContainers", []),
                "pod_containers": data.get("podContainers", []),
            }
        )
    return containers


def _find_vm_networks(
    vm_node_id: str, topology: dict, vni_map: dict, project_id: str = ""
) -> list[dict]:
    """Find networks connected to a VM via NIC handles."""
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    networks = []

    for edge in edges:
        handle = None
        network_node_id = None

        if edge.get("source") == vm_node_id:
            handle = edge.get("sourceHandle", "")
            network_node_id = edge.get("target")
        elif edge.get("target") == vm_node_id:
            handle = edge.get("targetHandle", "")
            network_node_id = edge.get("source")
        else:
            continue

        if not handle or not handle.startswith("nic-"):
            continue

        # Find the NIC data to get MAC address and model
        # Handle format: "nic-{nicId}-top" or "nic-{nicId}-bottom"
        vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
        mac = ""
        model = "virtio"
        if vm_node:
            for nic in vm_node.get("data", {}).get("nics", []):
                if nic["id"] in handle:
                    mac = nic.get("mac", "")
                    model = nic.get("model", "virtio")
                    break

        # BMC networks use a dedicated bridge (no VNI)
        net_node = next((n for n in nodes if n["id"] == network_node_id), None)
        if net_node and net_node.get("data", {}).get("networkType") == "bmc":
            # Use the NIC's MAC from the edge handle, otherwise generate one
            bmc_mac = mac  # mac was already resolved from the handle above
            if not bmc_mac:
                import random

                bmc_mac = "52:54:01:%02x:%02x:%02x" % (
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(0, 255),
                )
            networks.append(
                {
                    "bridge": f"br-bmc-{project_id[:8]}",
                    "mac": bmc_mac,
                    "nic_id": handle,
                    "model": model,
                }
            )
            continue

        if network_node_id not in vni_map:
            continue

        vni = vni_map[network_node_id]
        networks.append(
            {
                "bridge": f"br-{vni}",
                "mac": mac,
                "nic_id": handle,
                "model": model,
            }
        )

    return networks


def _find_container_networks(
    container_node_id: str, topology: dict, vni_map: dict, project_id: str = ""
) -> list[dict]:
    """Find networks connected to a container via NIC handles."""
    results: list[dict] = []
    container_node = next(
        (n for n in topology.get("nodes", []) if n["id"] == container_node_id), None
    )
    if not container_node:
        return results

    nics_by_id = {
        nic["id"]: nic for nic in container_node.get("data", {}).get("nics", [])
    }

    for edge in topology.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        src_h, tgt_h = edge.get("sourceHandle", ""), edge.get("targetHandle", "")

        nic_id = None
        net_node_id = None
        if src == container_node_id and src_h.startswith("nic-"):
            nic_id = src_h.split("-", 1)[1].rsplit("-", 1)[0]
            net_node_id = tgt
        elif tgt == container_node_id and tgt_h.startswith("nic-"):
            nic_id = tgt_h.split("-", 1)[1].rsplit("-", 1)[0]
            net_node_id = src

        if not nic_id or not net_node_id:
            continue

        nic = nics_by_id.get(nic_id, {})
        vni = vni_map.get(net_node_id)
        if not vni:
            continue

        net_node = next(
            (n for n in topology.get("nodes", []) if n["id"] == net_node_id), None
        )
        cidr = net_node.get("data", {}).get("cidr", "") if net_node else ""

        results.append(
            {
                "bridge": f"br-{vni}",
                "mac": nic.get("mac", ""),
                "nic_id": nic_id,
                "model": nic.get("model", "virtio"),
                "ip": nic.get("ip", ""),
                "cidr": cidr,
            }
        )

    return results


def _find_vm_name_by_ip(topology, ip):
    """Find the VM name that has a NIC with the given IP address."""
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        for nic in data.get("nics", []):
            if nic.get("ip") == ip:
                return data.get("name", node["id"][:8])
    return ip.replace(".", "-")


def _find_vm_disks(vm_node_id: str, topology: dict) -> list[dict]:
    """Find storage nodes connected to a VM via disk controller handles."""
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    disks = []

    for edge in edges:
        handle = None
        storage_node_id = None

        if edge.get("source") == vm_node_id:
            handle = edge.get("sourceHandle", "")
            storage_node_id = edge.get("target")
        elif edge.get("target") == vm_node_id:
            handle = edge.get("targetHandle", "")
            storage_node_id = edge.get("source")
        else:
            continue

        if not handle or not handle.startswith("dp-"):
            continue

        storage_node = next(
            (
                n
                for n in nodes
                if n["id"] == storage_node_id and n.get("type") == "storageNode"
            ),
            None,
        )
        if not storage_node:
            continue

        sdata = storage_node.get("data", {})

        # Find bus type from the disk controller
        vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
        bus = "virtio"
        if vm_node:
            for dc in vm_node.get("data", {}).get("diskControllers", []):
                if dc["id"] == handle:
                    bus = dc.get("bus", "virtio")
                    break

        disks.append(
            {
                "node_id": storage_node_id,
                "name": sdata.get("name", "disk"),
                "size_gb": sdata.get("size", 10),
                "format": sdata.get("format", "qcow2"),
                "bus": bus,
                "source": sdata.get("source", "blank"),
                "library_item_id": sdata.get("libraryItemId"),
                "patternId": sdata.get("patternId"),
                "patternDiskId": sdata.get("patternDiskId"),
                "snapshotItemId": sdata.get("snapshotItemId"),
            }
        )

    return disks


def _find_container_volumes(
    container_node_id: str, topology: dict, project_id: str, pool=None
) -> list[dict]:
    """Find storage nodes connected to a container via mount handles."""
    container_node = next(
        (n for n in topology.get("nodes", []) if n["id"] == container_node_id), None
    )
    if not container_node:
        return []

    mounts = container_node.get("data", {}).get("mounts", [])
    mounts_by_disk = {m["diskNodeId"]: m for m in mounts}

    results = []
    for edge in topology.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        src_h, tgt_h = edge.get("sourceHandle", ""), edge.get("targetHandle", "")

        disk_node_id = None
        if src == container_node_id and (tgt_h or "").startswith("mnt-"):
            disk_node_id = tgt
        elif tgt == container_node_id and (src_h or "").startswith("mnt-"):
            disk_node_id = src
        elif tgt == container_node_id and (tgt_h or "").startswith("mnt-"):
            disk_node_id = src
        elif src == container_node_id and (src_h or "").startswith("mnt-"):
            disk_node_id = tgt

        if not disk_node_id:
            continue

        disk_node = next(
            (
                n
                for n in topology.get("nodes", [])
                if n["id"] == disk_node_id and n.get("type") == "storageNode"
            ),
            None,
        )
        if not disk_node:
            continue

        mount_info = mounts_by_disk.get(disk_node_id, {})
        dd = disk_node.get("data", {})
        disk_path = _disk_path(project_id, container_node_id, disk_node_id, "raw", pool)
        mount_dir = os.path.join(_vm_dir(project_id, pool), f"mnt-{disk_node_id[:8]}")
        results.append(
            {
                "disk_path": disk_path,
                "mount_dir": mount_dir,
                "mount_path": mount_info.get("mountPath", "/data"),
                "size_gb": dd.get("size", 10),
                "node_id": disk_node_id,
            }
        )

    return results


# ── Script generators ──


def _vm_domain_name(project_id: str, node_id: str) -> str:
    return f"troshka-{project_id[:8]}-{node_id[:8]}"


def _extract_bmc_config(topology: dict, project_id: str) -> dict | None:
    """Extract BMC configuration from topology if any VMs have BMC enabled."""
    bmc_network = None
    for node in topology.get("nodes", []):
        if (
            node.get("type") == "networkNode"
            and node.get("data", {}).get("networkType") == "bmc"
        ):
            bmc_network = node
            break

    if not bmc_network:
        return None

    bmc_vms = []
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode" and node.get("data", {}).get("bmcEnabled"):
            bmc_ip = node["data"].get("bmcIp", "")
            if bmc_ip:
                bmc_vms.append(
                    {
                        "node_id": node["id"],
                        "domain_name": _vm_domain_name(project_id, node["id"]),
                        "bmc_ip": bmc_ip,
                    }
                )

    if not bmc_vms:
        return None

    # Collect DHCP hosts — VMs with a static IP on their BMC NIC
    dhcp_hosts = []
    bmc_net_id = bmc_network["id"]
    edges = topology.get("edges", [])
    nodes = topology.get("nodes", [])
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        for edge in edges:
            vm_id = node["id"]
            if edge.get("source") == vm_id:
                handle = edge.get("sourceHandle", "")
                net_id = edge.get("target")
            elif edge.get("target") == vm_id:
                handle = edge.get("targetHandle", "")
                net_id = edge.get("source")
            else:
                continue
            if net_id != bmc_net_id or not handle.startswith("nic-"):
                continue
            for nic in node.get("data", {}).get("nics", []):
                if nic["id"] in handle and nic.get("ip") and nic.get("mac"):
                    dhcp_hosts.append(
                        {
                            "mac": nic["mac"],
                            "ip": nic["ip"],
                            "name": node["data"].get("name", ""),
                        }
                    )

    return {
        "bmc_network": bmc_network["data"],
        "vms": bmc_vms,
        "dhcp_hosts": dhcp_hosts,
    }


def _setup_bmc_via_troshkad(host, project_id: str, bmc_config: dict):
    """Start BMC endpoints (Redfish + IPMI) on the host for this project."""
    from app.services.troshkad_client import start_job, wait_for_job

    try:
        _teardown_bmc_via_troshkad(host, project_id)
    except Exception:
        pass

    net_data = bmc_config["bmc_network"]
    cidr = net_data.get("cidr", "192.168.100.0/24")
    params = {
        "project_id": project_id,
        "bmc_cidr": cidr,
        "bmc_gateway_ip": cidr.rsplit(".", 1)[0] + ".1",
        "bmc_username": net_data.get("bmcUsername", "admin"),
        "bmc_password": net_data.get("bmcPassword", "password"),
        "vms": [
            {"domain_name": vm["domain_name"], "bmc_ip": vm["bmc_ip"]}
            for vm in bmc_config["vms"]
        ],
        "dhcp_hosts": bmc_config.get("dhcp_hosts", []),
    }
    job_id = start_job(host, "/bmc/setup", params)
    job = wait_for_job(host, job_id, timeout=120)
    if job["status"] == "failed":
        error = job.get("result", {}).get("error", "BMC setup failed")
        return error
    return True


def _teardown_bmc_via_troshkad(host, project_id: str):
    """Stop all BMC endpoints and remove BMC bridge for this project."""
    from app.services.troshkad_client import start_job, wait_for_job

    job_id = start_job(host, "/bmc/teardown", {"project_id": project_id})
    job = wait_for_job(host, job_id, timeout=60)
    if job["status"] == "failed":
        logger.warning(
            "BMC teardown failed for %s: %s", project_id[:8], job.get("result")
        )


def _vm_dir(project_id: str, pool=None) -> str:
    if pool and pool.mode.startswith("shared"):
        return f"/var/lib/troshka/shared/vms/{project_id}"
    return f"/var/lib/troshka/vms/{project_id}"


def _disk_path(
    project_id: str, vm_node_id: str, disk_node_id: str, fmt: str, pool=None
) -> str:
    return f"{_vm_dir(project_id, pool)}/{vm_node_id[:8]}-{disk_node_id[:8]}.{fmt}"


def _seed_path(project_id: str, vm_node_id: str, pool=None) -> str:
    return f"{_vm_dir(project_id, pool)}/{vm_node_id[:8]}-seed.iso"


def _image_cache_path(item_id: str, fmt: str, pool=None) -> str:
    if pool and pool.mode.startswith("shared"):
        return f"/var/lib/troshka/shared/images/{item_id}.{fmt}"
    return f"/var/lib/troshka/images/{item_id}.{fmt}"


def _pattern_cache_path(pattern_id: str, disk_id: str, fmt: str, pool=None) -> str:
    return f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{disk_id}.{fmt}"


def _snapshot_cache_path(item_id: str, disk_id: str, fmt: str) -> str:
    return f"/var/lib/troshka/cache/snapshots/{item_id}/{disk_id}.{fmt}"


def _resolve_boot_devs(vm: dict, vm_disks: list[dict], topology: dict) -> list[str]:
    boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
    all_nodes = topology.get("nodes", [])
    storage_nodes = {n["id"]: n for n in all_nodes if n.get("type") == "storageNode"}

    raw_boot_devs = vm.get("boot_devices") or None
    has_iso = any(d["format"] == "iso" for d in vm_disks)
    has_disk = any(d["format"] != "iso" for d in vm_disks)
    has_cdrom_controller = any(
        dc.get("bus") == "sata" and "cdrom" in dc.get("name", "")
        for dc in vm.get("disk_controllers", [])
    )
    if raw_boot_devs is None or (raw_boot_devs == ["hd"] and has_iso):
        if has_iso and has_disk:
            return ["cdrom", "hd"]
        elif has_iso:
            return ["cdrom"]
        elif has_disk:
            return ["hd"]
        else:
            return ["network"]
    boot_devs = []
    seen = set()
    for d in raw_boot_devs:
        if d in boot_type_map:
            dev = boot_type_map[d]
        elif d in storage_nodes:
            dev = (
                "cdrom"
                if storage_nodes[d].get("data", {}).get("format") == "iso"
                else "hd"
            )
        else:
            continue
        if dev not in seen:
            boot_devs.append(dev)
            seen.add(dev)
    # Add cdrom fallback if VM has a cdrom controller but no cdrom in boot order
    if has_cdrom_controller and "cdrom" not in seen:
        boot_devs.append("cdrom")
    return boot_devs or ["hd"]


def diff_topologies(current: dict, deployed: dict) -> dict:
    """Diff current topology against what was deployed. Returns changes."""
    cur_nodes = {n["id"]: n for n in current.get("nodes", [])}
    dep_nodes = {n["id"]: n for n in deployed.get("nodes", [])}

    added_vms = []
    removed_vms = []
    changed_vms = []
    added_networks = []
    removed_networks = []

    for nid, node in cur_nodes.items():
        if nid not in dep_nodes:
            if node.get("type") == "vmNode":
                added_vms.append(node)
            elif node.get("type") == "networkNode":
                added_networks.append(node)

    for nid, node in dep_nodes.items():
        if nid not in cur_nodes:
            if node.get("type") == "vmNode":
                removed_vms.append(node)
            elif node.get("type") == "networkNode":
                removed_networks.append(node)

    skip_keys = {"status", "redeployStep", "redeployDetail", "liveBootDevs"}
    for nid, node in cur_nodes.items():
        if nid in dep_nodes and node.get("type") == "vmNode":
            cur_data = {
                k: v for k, v in node.get("data", {}).items() if k not in skip_keys
            }
            dep_data = {
                k: v
                for k, v in dep_nodes[nid].get("data", {}).items()
                if k not in skip_keys
            }
            if cur_data != dep_data:
                changed_vms.append(node)

    return {
        "added_vms": added_vms,
        "removed_vms": removed_vms,
        "changed_vms": changed_vms,
        "added_networks": added_networks,
        "removed_networks": removed_networks,
        "has_changes": bool(
            added_vms
            or removed_vms
            or changed_vms
            or added_networks
            or removed_networks
        ),
    }


def cache_library_images(topology: dict, host, db_session, progress_callback=None):
    """Download all library images and pattern disks to host cache via troshkad.

    Uses troshkad images/cache endpoint for each item. Downloads run in parallel
    as separate jobs on the host agent.

    Args:
        topology: Project topology dict
        host: Host model instance
        db_session: SQLAlchemy session
        progress_callback: optional callback(downloaded_bytes, total_bytes)
    """
    from app.models.library import LibraryItem
    from app.models.pattern import Pattern, PatternDisk
    from app.services import s3_storage
    from app.services.troshkad_client import poll_job

    pool = _get_host_pool(host, db_session)
    nodes = topology.get("nodes", [])
    items_to_cache = []

    # Collect library items
    for node in nodes:
        if node.get("type") != "storageNode":
            continue
        item_id = node.get("data", {}).get("libraryItemId")
        if item_id:
            item = db_session.query(LibraryItem).filter_by(id=item_id).first()
            if not item:
                item_name = node.get("data", {}).get("libraryItemName")
                fmt = node.get("data", {}).get("format", "qcow2")
                if item_name:
                    from sqlalchemy import func as sa_func

                    item = (
                        db_session.query(LibraryItem)
                        .filter(
                            sa_func.lower(LibraryItem.name) == item_name.lower(),
                            LibraryItem.format == fmt,
                        )
                        .first()
                    )
                    if item:
                        logger.info(
                            "Library item %s not found by ID, resolved by name '%s' → %s",
                            item_id[:8],
                            item_name,
                            item.id[:8],
                        )
                        node["data"]["libraryItemId"] = item.id
                        item_id = item.id
            if item and item.s3_key:
                fmt = node.get("data", {}).get("format", "qcow2")
                cache_path = _image_cache_path(item_id, fmt, pool)
                items_to_cache.append(
                    {
                        "item_id": item_id,
                        "name": item.name,
                        "s3_key": item.s3_key,
                        "cache_path": cache_path,
                        "expected_size": item.size_bytes,
                        "source": getattr(item, "source", "local"),
                        "source_provider_id": getattr(item, "source_provider_id", None),
                    }
                )

    # Collect PXE boot ISOs from VM nodes
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        item_id = node.get("data", {}).get("pxeBootIsoId")
        if item_id:
            item = db_session.query(LibraryItem).filter_by(id=item_id).first()
            if item and item.s3_key:
                cache_path = _image_cache_path(item_id, "iso", pool)
                items_to_cache.append(
                    {
                        "item_id": item_id,
                        "name": item.name,
                        "s3_key": item.s3_key,
                        "cache_path": cache_path,
                        "expected_size": item.size_bytes,
                        "source": getattr(item, "source", "local"),
                        "source_provider_id": getattr(item, "source_provider_id", None),
                    }
                )

    # Collect pattern disks
    for node in nodes:
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        pattern_id = data.get("patternId")
        pattern_disk_id = data.get("patternDiskId")
        if pattern_id and pattern_disk_id:
            pd = (
                db_session.query(PatternDisk)
                .filter_by(id=pattern_disk_id, pattern_id=pattern_id)
                .first()
            )
            if pd and pd.s3_key:
                cache_path = _pattern_cache_path(
                    pattern_id, pd.source_disk_id, pd.format, pool
                )
                disk_name = (
                    data.get("label") or data.get("name") or node.get("id", "")[:8]
                )
                pattern_obj = db_session.query(Pattern).filter_by(id=pattern_id).first()
                pattern_tags = (pattern_obj.tags or {}) if pattern_obj else {}
                items_to_cache.append(
                    {
                        "item_id": pattern_disk_id,
                        "name": disk_name,
                        "s3_key": pd.s3_key,
                        "cache_path": cache_path,
                        "expected_size": pd.size_bytes,
                        "source": pattern_tags.get("source", "local"),
                        "source_provider_id": pattern_tags.get("source_provider_id"),
                    }
                )

    seen_ids = set()
    deduped = []
    for ic in items_to_cache:
        if ic["item_id"] not in seen_ids:
            seen_ids.add(ic["item_id"])
            deduped.append(ic)
    items_to_cache = deduped

    logger.info("cache_library_images: %d items to cache", len(items_to_cache))
    if not items_to_cache:
        return

    # For shared pools: skip items already cached, coordinate downloads
    # Only use SharedCacheEntry for items on shared storage (not local pattern cache)
    if pool and pool.mode.startswith("shared"):
        items_needing_download = []
        for ic in items_to_cache:
            if ic["cache_path"].startswith("/var/lib/troshka/local/"):
                items_needing_download.append(ic)
                continue
            status, entry = _check_shared_cache(
                db_session, pool, ic["item_id"], "image"
            )
            if status == "ready":
                try:
                    jid = start_job(host, "/files/stat", {"path": ic["cache_path"]})
                    stat_job = wait_for_job(host, jid, timeout=10)
                    if stat_job.get("result", {}).get("exists"):
                        logger.info(
                            "  %s already on shared storage, skipping", ic["name"]
                        )
                        continue
                except TroshkadError:
                    pass
                logger.warning(
                    "  %s cache entry says ready but file missing, re-downloading",
                    ic["name"],
                )
                if entry:
                    db_session.delete(entry)
                    db_session.commit()
            elif status == "downloading":
                logger.info(
                    "  %s being downloaded by another host, waiting...", ic["name"]
                )
                if _wait_for_shared_cache(db_session, pool.id, ic["item_id"], "image"):
                    logger.info("  %s now available on shared storage", ic["name"])
                    continue
                else:
                    logger.warning("  %s download timed out, will retry", ic["name"])
            # Need to download — create/update cache entry
            rel_path = ic["cache_path"].replace("/var/lib/troshka/shared/", "")
            _create_shared_cache_entry(
                db_session, pool, ic["item_id"], "image", rel_path
            )
            items_needing_download.append(ic)
        items_to_cache = items_needing_download

    # Check which items already exist on host (local cache)
    items_to_download = []
    for ic in items_to_cache:
        try:
            jid = start_job(host, "/files/stat", {"path": ic["cache_path"]})
            stat_job = wait_for_job(host, jid, timeout=10)
            if stat_job.get("result", {}).get("exists"):
                logger.info("  %s already cached locally, skipping", ic["name"])
                continue
        except TroshkadError:
            pass
        items_to_download.append(ic)

    if not items_to_download:
        logger.info("  all items cached, no downloads needed")
        return

    # Start download jobs using aws s3 cp
    from app.services.s3_storage import _get_readonly_s3_config, _get_s3_config

    s3_creds = _get_s3_config()
    s3_bucket = s3_storage._bucket()
    central_creds = _get_readonly_s3_config()
    active_jobs = []
    for ic in items_to_download:
        if ic.get("source") == "central" and central_creds:
            dl_creds = central_creds
            dl_bucket = central_creds["bucket"]
        else:
            dl_creds = s3_creds
            dl_bucket = s3_bucket
        s3_url = f"s3://{dl_bucket}/{ic['s3_key']}"
        try:
            job_id = start_job(
                host,
                "/images/cache",
                {
                    "s3_url": s3_url,
                    "dest_path": ic["cache_path"],
                    "expected_size": ic.get("expected_size", 0),
                    "expected_format": (
                        "qcow2" if ic["cache_path"].endswith(".qcow2") else None
                    ),
                    "aws_access_key_id": dl_creds.get("access_key_id", ""),
                    "aws_secret_access_key": dl_creds.get("secret_access_key", ""),
                    "aws_region": dl_creds.get("region", "us-east-1"),
                    "aws_endpoint_url": dl_creds.get("endpoint_url", ""),
                },
            )
            active_jobs.append(
                {
                    "job_id": job_id,
                    "name": ic["name"],
                    "item_id": ic["item_id"],
                    "expected_size": ic.get("expected_size", 0),
                }
            )
            logger.info(
                "  cache job started: %s (%s) -> %s",
                ic["name"],
                ic["item_id"][:8],
                ic["cache_path"],
            )
        except TroshkadError as e:
            logger.error("Failed to start cache job for %s: %s", ic["name"], e)

    if not active_jobs:
        return

    # Poll until all jobs complete
    completed: set[str] = set()
    failed: set[str] = set()
    stale_polls = 0
    last_completed_count = 0

    while len(completed) + len(failed) < len(active_jobs):
        _time.sleep(5)
        for aj in active_jobs:
            if aj["job_id"] in completed or aj["job_id"] in failed:
                continue
            try:
                job = poll_job(host, aj["job_id"])
                if job["status"] == "completed":
                    completed.add(aj["job_id"])
                    logger.info("cache: %s downloaded", aj["name"])
                    if pool and pool.mode.startswith("shared"):
                        _mark_shared_cache_ready(
                            db_session, pool.id, aj["item_id"], "image"
                        )
                elif job["status"] == "failed":
                    failed.add(aj["job_id"])
                    logger.error(
                        "cache: %s failed: %s",
                        aj["name"],
                        job.get("result", {}).get("error", ""),
                    )
                    if pool and pool.mode.startswith("shared"):
                        _mark_shared_cache_error(
                            db_session, pool.id, aj["item_id"], "image"
                        )
            except TroshkadError:
                pass  # Transient connection error, retry next poll

        if progress_callback:
            done_count = len(completed) + len(failed)
            items = []
            for aj in active_jobs:
                exp = aj.get("expected_size", 0)
                size_str = f"{exp / (1024**3):.1f} GB" if exp else ""
                if aj["job_id"] in completed:
                    items.append(
                        f"{aj['name']}: done{f' ({size_str})' if size_str else ''}"
                    )
                elif aj["job_id"] in failed:
                    items.append(f"{aj['name']}: failed")
                else:
                    downloaded_gb = 0.0
                    try:
                        job = poll_job(host, aj["job_id"])
                        for line in reversed(job.get("output", [])):
                            line = line.strip()
                            if "Downloading:" in line and "GB" in line:
                                try:
                                    downloaded_gb = float(
                                        line.split("Downloading:")[1]
                                        .strip()
                                        .replace("GB", "")
                                        .strip()
                                    )
                                except (ValueError, IndexError):
                                    pass
                                break
                    except TroshkadError:
                        pass
                    exp = aj.get("expected_size", 0)
                    total_gb = exp / (1024**3) if exp else 0
                    if downloaded_gb > 0 and total_gb > 0:
                        pct = min(99, int(downloaded_gb / total_gb * 100))
                        items.append(
                            f"{aj['name']}: {downloaded_gb:.1f} / {total_gb:.1f} GB ({pct}%)"
                        )
                    elif total_gb > 0:
                        items.append(f"{aj['name']}: downloading {total_gb:.1f} GB...")
                    else:
                        items.append(f"{aj['name']}: downloading...")
            progress_callback(f"{done_count}/{len(active_jobs)}", items)

        if len(completed) + len(failed) == last_completed_count:
            stale_polls += 1
        else:
            stale_polls = 0
            last_completed_count = len(completed) + len(failed)

        if stale_polls >= 720:  # 1 hour with no progress
            logger.error("Download stalled for 1 hour, aborting")
            return

    if failed:
        logger.error(
            "cache_library_images: %d/%d downloads failed",
            len(failed),
            len(active_jobs),
        )


# ── Async orchestrators ──


def _setup_networks_via_troshkad(host, topology, vni_map, db_session, project_id):
    """Set up full VXLAN mesh networking via troshkad.

    Builds the network config and sends it to the networks/full-setup endpoint.
    Returns True on success, error string on failure.
    """
    from app.services.vxlan import build_host_network_config

    all_hosts = db_session.query(Host).filter(Host.state == "active").all()
    peer_ips = [h.ip_address for h in all_hosts if h.ip_address]
    network_config = build_host_network_config(topology, vni_map, peer_ips)

    # If LB is present and external, add its frontend ports as port forwards to gateway
    lb = network_config.get("loadbalancer")
    if lb and lb.get("frontends") and lb.get("external", True):
        gw = network_config.get("gateway")
        if not gw:
            # Create minimal gateway config for LB port forwarding
            first_vni = next(iter(vni_map.values()), None)
            if first_vni:
                from app.services.vxlan import _transit_subnet

                transit = _transit_subnet(first_vni)
                network_config["gateway"] = {
                    "name": "lb-gateway",
                    "mode": "nat-portforward",
                    "outbound_policy": "allow-all",
                    "outbound_ports": "",
                    "port_forwards": [],
                    "eip_private_ips": [],
                    "transit_ns_ip": transit["ns_ip"],
                }
            gw = network_config.get("gateway")
        if gw:
            if gw.get("mode") not in ("nat", "nat-portforward"):
                gw["mode"] = "nat-portforward"
            pf_list = gw.get("port_forwards", [])
            # Find the EIP private IP for the LB's extIpId
            lb_eip_priv = ""
            lb_ext_ip_id = lb.get("ext_ip_id", "")
            if lb_ext_ip_id:
                ext_ips = topology.get("externalIps", [])
                for eip in ext_ips:
                    if eip.get("id") == lb_ext_ip_id and eip.get("_private_ip"):
                        lb_eip_priv = eip["_private_ip"]
                        break
            if not lb_eip_priv:
                eip_priv_ips = gw.get("eip_private_ips", [])
                lb_eip_priv = eip_priv_ips[0] if eip_priv_ips else ""
            for fe in lb["frontends"]:
                pf_list.append(
                    {
                        "extPort": fe["bindPort"],
                        "intIp": gw.get("transit_ns_ip", ""),
                        "intPort": fe["bindPort"],
                        "_private_ip": lb_eip_priv,
                    }
                )
            gw["port_forwards"] = pf_list

    # Build params for troshkad
    params = {
        "project_id": project_id,
        "host_ip": host.ip_address,
        "networks": network_config.get("networks", []),
        "gateway": network_config.get("gateway"),
        "routers": network_config.get("routers", []),
    }

    try:
        job_id = start_job(host, "/networks/full-setup", params)
        job = wait_for_job(host, job_id, timeout=120)
        if job["status"] == "failed":
            error = job.get("result", {}).get("error", "Network setup failed")
            return f"Network setup failed: {error}"
        return True
    except TroshkadError as e:
        return f"Network setup failed: {e}"


def _teardown_networks_via_troshkad(host, project_id, vni_map):
    """Tear down project networking via troshkad."""
    vni_list = list(vni_map.values()) if vni_map else []
    try:
        job_id = start_job(
            host,
            "/networks/full-teardown",
            {
                "project_id": project_id,
                "vni_list": vni_list,
            },
        )
        wait_for_job(host, job_id, timeout=60)
    except TroshkadError as e:
        logger.warning("Network teardown error for %s: %s", project_id[:8], e)


def _setup_pxe_via_troshkad(host, topology, vni_map, project_id=""):
    """Set up PXE boot services for managed-mode PXE networks.

    Extracts kernel/initrd from cached ISOs and starts HTTP install source
    server inside the network namespace.
    """
    from app.services.vxlan import build_host_network_config

    network_config = build_host_network_config(topology, vni_map, [])

    for net in network_config.get("networks", []):
        pxe = net.get("pxe_config")
        if not pxe or pxe.get("server_mode") != "builtin":
            continue
        iso_path = pxe.get("iso_path")
        if not iso_path:
            continue

        gateway_ip = ""
        dhcp_config = net.get("dhcp_config", {})
        if dhcp_config:
            gateway_ip = dhcp_config.get("gateway", "")

        try:
            job_id = start_job(
                host,
                "/pxe/setup",
                {
                    "project_id": project_id,
                    "vni": net["vni"],
                    "iso_path": iso_path,
                    "gateway_ip": gateway_ip,
                    "http_port": pxe.get("http_port", 8080),
                    "tftp_root": pxe.get("tftp_root", ""),
                },
            )
            job = wait_for_job(host, job_id, timeout=120)
            if job["status"] == "failed":
                logger.error(
                    "PXE setup failed for VNI %s: %s",
                    net["vni"],
                    job.get("result", {}).get("error", ""),
                )
        except TroshkadError as e:
            logger.error("PXE setup failed for VNI %s: %s", net["vni"], e)


def _create_seed_isos_via_troshkad(host, project_id, topology, pool=None):
    """Create cloud-init seed ISOs via troshkad seeds/create-batch."""
    from app.services.cloud_init import generate_metadata, generate_userdata

    nodes = topology.get("nodes", [])
    seeds = []
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if not data.get("cloudInit"):
            continue

        node_id = node["id"]
        vm_label = data.get("name", "vm")
        userdata = generate_userdata(data)
        metadata = generate_metadata(vm_label)
        path = _seed_path(project_id, node_id, pool)

        seed = {
            "path": path,
            "user_data": userdata,
            "meta_data": metadata,
        }
        network_config = data.get("ciNetworkConfig", "")
        if network_config:
            seed["network_config"] = network_config
        seeds.append(seed)

    if not seeds:
        return

    try:
        job_id = start_job(host, "/seeds/create-batch", {"seeds": seeds})
        job = wait_for_job(host, job_id, timeout=60)
        if job["status"] == "failed":
            logger.error(
                "Seed ISO creation failed: %s", job.get("result", {}).get("error", "")
            )
    except TroshkadError as e:
        logger.error("Seed ISO creation failed: %s", e)


def _create_vm_disks_via_troshkad(host, project_id, vm, vm_disks, pool=None):
    """Create disk images for a VM via troshkad disks/create. Returns list of job IDs."""
    job_ids = []
    for disk in vm_disks:
        if disk["format"] == "iso":
            continue
        dp = _disk_path(
            project_id, vm["node_id"], disk["node_id"], disk["format"], pool
        )

        backing = None
        if (
            disk.get("source") == "pattern"
            and disk.get("patternId")
            and disk.get("patternDiskId")
        ):
            from app.core.database import SessionLocal as _SL
            from app.models.pattern import PatternDisk as _PD

            _s = _SL()
            _pd = _s.query(_PD).filter_by(id=disk["patternDiskId"]).first()
            _cache_disk_id = _pd.source_disk_id if _pd else disk["patternDiskId"]
            _s.close()
            backing = _pattern_cache_path(
                disk["patternId"], _cache_disk_id, disk["format"], pool
            )
        elif disk.get("source") == "snapshot" and disk.get("snapshotItemId"):
            from app.core.database import SessionLocal as _SL2
            from app.models.library import LibraryItemDisk as _LID

            _s2 = _SL2()
            _snap_disks = (
                _s2.query(_LID)
                .filter_by(
                    library_item_id=disk["snapshotItemId"], format=disk["format"]
                )
                .order_by(_LID.boot_order)
                .all()
            )
            if _snap_disks:
                s3_key = _snap_disks[0].s3_key
                parts = s3_key.rsplit("/", 1)[-1].rsplit(".", 1)
                orig_disk_id = parts[0] if parts else _snap_disks[0].id
                backing = _snapshot_cache_path(
                    disk["snapshotItemId"], orig_disk_id, disk["format"]
                )
            _s2.close()
        elif disk.get("source") == "library" and disk.get("library_item_id"):
            backing = _image_cache_path(disk["library_item_id"], disk["format"], pool)

        params = {
            "path": dp,
            "size_gb": disk["size_gb"],
            "format": disk["format"],
        }
        if backing:
            params["backing_file"] = backing

        job_id = start_job(host, "/disks/create", params, request_timeout=60)
        job_ids.append(job_id)
    return job_ids


def _create_vm_via_troshkad(
    host,
    project_id,
    vm,
    topology,
    vni_map,
    pool=None,
    disk_cache=None,
    clock_offset=None,
):
    """Create a VM definition via troshkad vms/create."""
    vm_name = _vm_domain_name(project_id, vm["node_id"])
    vm_disks = _find_vm_disks(vm["node_id"], topology)
    vm_networks = _find_vm_networks(vm["node_id"], topology, vni_map, project_id)

    # Build disk list for virt-install
    vm_dir = _vm_dir(project_id, pool)
    disks = []
    for disk in vm_disks:
        if disk["format"] == "iso":
            if disk.get("library_item_id"):
                cache_path = _image_cache_path(disk["library_item_id"], "iso", pool)
                link_path = (
                    f"{vm_dir}/{vm['node_id'][:8]}-{disk['library_item_id'][:8]}.iso"
                )
                disks.append(
                    {
                        "path": link_path,
                        "bus": "sata",
                        "device": "cdrom",
                        "symlink_from": cache_path,
                    }
                )
            continue
        dp = _disk_path(
            project_id, vm["node_id"], disk["node_id"], disk["format"], pool
        )
        disks.append({"path": dp, "bus": disk["bus"]})

    # Seed ISO as cdrom
    if vm.get("cloud_init"):
        disks.append(
            {
                "path": _seed_path(project_id, vm["node_id"], pool),
                "bus": "sata",
                "device": "cdrom",
            }
        )

    # Build network list
    networks = []
    for net in vm_networks:
        entry = {"bridge": net["bridge"], "model": net.get("model", "virtio")}
        if net["mac"]:
            entry["mac"] = net["mac"]
        networks.append(entry)

    # Translate canvas boot device IDs to libvirt boot types
    boot_devs = []
    seen_boot = set()
    all_nodes = {n["id"]: n for n in topology.get("nodes", [])}
    for dev in vm.get("boot_devices", []):
        if dev == "network":
            bt = "network"
        else:
            snode = all_nodes.get(dev)
            if snode and snode.get("type") == "storageNode":
                bt = "cdrom" if snode.get("data", {}).get("format") == "iso" else "hd"
            else:
                bt = "hd"
        if bt not in seen_boot:
            boot_devs.append(bt)
            seen_boot.add(bt)

    params = {
        "domain_name": vm_name,
        "uuid": vm.get("uuid") or vm["node_id"],
        "vcpus": vm["vcpus"],
        "ram_mb": vm["ram_gb"] * 1024,
        "disks": disks,
        "networks": networks,
        "firmware": vm.get("firmware", "bios"),
        "secure_boot": vm.get("secure_boot", False),
        "boot_devs": boot_devs,
        "video_model": vm.get("video_model", "virtio"),
        "input_model": vm.get("input_model", "virtio"),
    }
    if disk_cache:
        params["disk_cache"] = disk_cache
    if clock_offset is not None:
        params["clock_offset"] = clock_offset

    job_id = start_job(host, "/vms/create", params)
    return job_id


def _setup_metadata_via_troshkad(host, project_id, topology, vni_map):
    """Deploy the cloud-init metadata service via troshkad metadata/deploy."""
    from app.services.cloud_init import generate_metadata, generate_userdata

    nodes = topology.get("nodes", [])
    vm_configs = {}
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if not data.get("cloudInit"):
            continue
        vm_label = data.get("name", "vm")
        userdata = generate_userdata(data)
        metadata = generate_metadata(vm_label)
        for nic in data.get("nics", []):
            mac = nic.get("mac", "").lower()
            if mac:
                vm_configs[mac] = {
                    "vm_name": vm_label,
                    "userdata": userdata,
                    "metadata": metadata,
                }

    if not vm_configs:
        return

    bridges = [f"br-{vni}" for vni in vni_map.values()]
    ns = f"troshka-{project_id[:8]}"

    try:
        job_id = start_job(
            host,
            "/metadata/deploy",
            {
                "project_id": project_id,
                "bridges": bridges,
                "vm_configs": vm_configs,
                "namespace": ns,
            },
        )
        wait_for_job(host, job_id, timeout=30)
        logger.info("Metadata service deployed for %s", project_id[:8])
    except TroshkadError as e:
        logger.warning(
            "Metadata service deployment failed for %s: %s", project_id[:8], e
        )


def _start_vms_via_troshkad(host, project_id, topology):
    """Start VMs respecting start order via troshkad vms/start.
    Returns list of (vm_name, error) for any VMs that failed to start."""
    vms = _extract_vms(topology)
    start_order = topology.get("startOrder", [])
    failed = []

    ordered_vm_ids = set()
    if start_order:
        for entry in start_order:
            vm_id = entry.get("vmId", "")
            vm = next((v for v in vms if v["node_id"] == vm_id), None)
            if vm:
                ordered_vm_ids.add(vm_id)
                if entry.get("autoStart", True) is False:
                    logger.info(
                        "Deploy %s: skipping %s (auto-start disabled)",
                        project_id[:8],
                        vm["name"],
                    )
                    continue
                delay = entry.get("delaySeconds", 0)
                if delay > 0:
                    _time.sleep(delay)
                vm_name = _vm_domain_name(project_id, vm["node_id"])
                try:
                    job_id = start_job(host, "/vms/start", {"domain_name": vm_name})
                    wait_for_job(host, job_id, timeout=120)
                except TroshkadError as e:
                    logger.warning("Failed to start VM %s: %s", vm_name, e)
                    failed.append((vm["name"], str(e)))

    # Start any VMs not in start order (parallel), skip VMs with powerOnAtDeploy=false
    power_on_map = {}
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode":
            power_on_map[node["id"]] = node.get("data", {}).get("powerOnAtDeploy", True)

    unordered_jobs = []
    for vm in vms:
        if vm["node_id"] not in ordered_vm_ids:
            if not power_on_map.get(vm["node_id"], True):
                logger.info(
                    "Deploy %s: skipping %s (powerOnAtDeploy=false)",
                    project_id[:8],
                    vm["name"],
                )
                continue
            vm_name = _vm_domain_name(project_id, vm["node_id"])
            try:
                job_id = start_job(host, "/vms/start", {"domain_name": vm_name})
                unordered_jobs.append((vm["name"], vm_name, job_id))
            except TroshkadError as e:
                logger.warning("Failed to start VM %s: %s", vm_name, e)
                failed.append((vm["name"], str(e)))
    for name, vm_name, job_id in unordered_jobs:
        try:
            wait_for_job(host, job_id, timeout=120)
        except TroshkadError as e:
            logger.warning("Failed to start VM %s: %s", vm_name, e)
            failed.append((name, str(e)))

    return failed


def _project_deleted(project_id: str) -> bool:
    """Check if a project was deleted mid-deploy."""
    from app.core.database import SessionLocal
    from app.models.project import Project

    check_s = SessionLocal()
    try:
        return check_s.query(Project).filter_by(id=project_id).first() is None
    finally:
        check_s.close()


def _auto_assign_container_ips(topology: dict) -> None:
    """Assign IPs to container NICs that don't have static IPs.

    Mutates topology in-place. Picks IPs from the connected network's CIDR,
    avoiding all IPs already used by VMs or other containers.
    """
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    used_ips = _collect_used_ips(topology)

    # Also reserve .1 (gateway) and DHCP range for each network
    net_nodes = {n["id"]: n for n in nodes if n.get("type") == "networkNode"}

    for node in nodes:
        if node.get("type") != "containerNode":
            continue
        data = node.get("data", {})
        for nic in data.get("nics", []):
            if nic.get("ip"):
                continue

            # Find connected network via edges
            nic_handle_top = f"nic-{nic['id']}-top"
            nic_handle_bottom = f"nic-{nic['id']}-bottom"
            net_node = None
            for edge in edges:
                src, tgt = edge.get("source"), edge.get("target")
                sh, th = edge.get("sourceHandle", ""), edge.get("targetHandle", "")
                if src == node["id"] and sh in (nic_handle_top, nic_handle_bottom):
                    net_node = net_nodes.get(tgt)
                elif tgt == node["id"] and th in (nic_handle_top, nic_handle_bottom):
                    net_node = net_nodes.get(src)
                if net_node:
                    break

            if not net_node:
                continue

            cidr = net_node.get("data", {}).get("cidr", "")
            if not cidr:
                continue

            net_data = net_node.get("data", {})
            dhcp_range = _get_dhcp_range(net_data)
            if not dhcp_range:
                continue
            start_int, end_int = dhcp_range
            for addr_int in range(start_int, end_int + 1):
                candidate_str = str(ipaddress.ip_address(addr_int))
                if candidate_str not in used_ips:
                    nic["ip"] = candidate_str
                    used_ips.add(candidate_str)
                    logger.info(
                        "Auto-assigned %s to container %s NIC %s (from DHCP range)",
                        candidate_str,
                        data.get("name"),
                        nic.get("name"),
                    )
                    break


def _collect_used_ips(topology: dict) -> set[str]:
    """Collect all IPs already assigned: static IPs on VMs/containers + gateway IPs."""
    used = set()
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        for nic in data.get("nics", []):
            ip = nic.get("ip", "")
            if ip:
                used.add(ip)
        if node.get("type") == "networkNode":
            cidr = data.get("cidr", "")
            if cidr:
                try:
                    net = ipaddress.ip_network(cidr, strict=False)
                    used.add(str(net.network_address + 1))
                except ValueError:
                    pass
    return used


def _get_dhcp_range(net_data: dict) -> tuple[int, int] | None:
    """Return the DHCP range as (start_int, end_int) for a network node's data.

    Matches the auto-generation logic in vxlan.py: hosts[9] to hosts[-1].
    """
    range_start = net_data.get("dhcpRangeStart", "")
    range_end = net_data.get("dhcpRangeEnd", "")
    if not range_start or not range_end:
        cidr = net_data.get("cidr", "")
        if cidr:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                hosts = list(net.hosts())
                if len(hosts) > 10:
                    if not range_start:
                        range_start = str(hosts[min(9, len(hosts) - 2)])
                    if not range_end:
                        range_end = str(hosts[-1])
            except ValueError:
                pass
    if range_start and range_end:
        try:
            return (
                int(ipaddress.ip_address(range_start)),
                int(ipaddress.ip_address(range_end)),
            )
        except ValueError:
            pass
    return None


def _create_and_start_container(host, project_id, ctr, topology, vni_map, pool=None):
    """Create and start a container via troshkad."""
    container_name = f"troshka-{project_id[:8]}-{ctr['node_id'][:8]}"
    networks = _find_container_networks(ctr["node_id"], topology, vni_map, project_id)
    volumes = _find_container_volumes(ctr["node_id"], topology, project_id, pool)

    create_params = {
        "container_name": container_name,
        "image": ctr["image"],
        "cpus": ctr["cpus"],
        "memory_mb": ctr["memory_mb"],
        "env_vars": ctr["env_vars"],
        "ports": ctr["ports"],
        "networks": [
            {
                "bridge": n["bridge"],
                "ip": n.get("ip"),
                "mac": n.get("mac"),
                "cidr": n.get("cidr"),
            }
            for n in networks
        ],
        "volumes": [
            {
                "disk_path": v["disk_path"],
                "mount_dir": v["mount_dir"],
                "mount_path": v["mount_path"],
            }
            for v in volumes
        ],
        "command": ctr.get("command"),
        "restart_policy": ctr.get("restart_policy", "always"),
        "privileged": ctr.get("privileged", False),
    }
    job_id = start_job(host, "/containers/create", create_params)
    wait_for_job(host, job_id, timeout=120)

    job_id = start_job(host, "/containers/start", {"container_name": container_name})
    wait_for_job(host, job_id, timeout=30)


def _create_and_start_pod(host, project_id, ctr, topology, vni_map, pool=None):
    """Create and start a pod via troshkad."""
    pod_name = ctr["name"]
    networks = _find_container_networks(ctr["node_id"], topology, vni_map, project_id)
    volumes = _find_container_volumes(ctr["node_id"], topology, project_id, pool)

    vol_by_disk = {v["node_id"]: v for v in volumes}

    def _resolve_mounts(sub_mounts):
        result = []
        for m in sub_mounts:
            vol = vol_by_disk.get(m.get("diskNodeId", ""))
            if vol:
                result.append(f"{vol['mount_dir']}:{m.get('mountPath', '/data')}")
        return result

    create_params = {
        "project_id": project_id,
        "pod_name": pod_name,
        "networks": [
            {
                "bridge": n["bridge"],
                "ip": n.get("ip"),
                "mac": n.get("mac"),
                "cidr": n.get("cidr"),
            }
            for n in networks
        ],
        "init_containers": [
            {
                "name": ic["name"],
                "image": ic.get("image", ""),
                "env": {
                    ev["key"]: ev["value"]
                    for ev in ic.get("envVars", [])
                    if ev.get("key")
                },
                "mounts": _resolve_mounts(ic.get("mounts", [])),
                "command": ic.get("command"),
            }
            for ic in ctr.get("init_containers", [])
        ],
        "containers": [
            {
                "name": pc["name"],
                "image": pc.get("image", ""),
                "cpus": pc.get("cpus", 1),
                "memory": pc.get("memory", 512),
                "env": {
                    ev["key"]: ev["value"]
                    for ev in pc.get("envVars", [])
                    if ev.get("key")
                },
                "mounts": _resolve_mounts(pc.get("mounts", [])),
                "command": pc.get("command"),
            }
            for pc in ctr.get("pod_containers", [])
        ],
        "restart_policy": ctr.get("restart_policy", "always"),
        "privileged": ctr.get("privileged", False),
    }
    job_id = start_job(host, "/pods/create", create_params)
    wait_for_job(host, job_id, timeout=120)

    full_pod_name = f"troshka-{project_id[:8]}-{pod_name}"
    job_id = start_job(host, "/pods/start", {"pod_name": full_pod_name})
    wait_for_job(host, job_id, timeout=120)


# ---------------------------------------------------------------------------
# KubeVirt native deploy helpers (extracted to reduce cognitive complexity)
# ---------------------------------------------------------------------------


def _setup_kubevirt_s3_clients():
    """Set up primary and central S3 clients for KubeVirt deploy."""
    from app.services.s3_storage import (
        _get_readonly_s3_config,
        _get_s3_config,
        owner_params,
    )

    s3_config = _get_s3_config()
    central_s3_config = _get_readonly_s3_config()

    s3_client = boto3.client(
        "s3",
        region_name=s3_config.get("region", "us-east-1"),
        aws_access_key_id=s3_config.get("access_key_id", ""),
        aws_secret_access_key=s3_config.get("secret_access_key", ""),
        endpoint_url=s3_config.get("endpoint_url") or None,
    )
    bucket = s3_config.get("bucket", "troshka-images")
    s3_op = owner_params(s3_config)

    central_s3_client = None
    central_bucket = ""
    central_op: dict = {}
    if central_s3_config:
        central_s3_client = boto3.client(
            "s3",
            region_name=central_s3_config.get("region", "us-east-1"),
            aws_access_key_id=central_s3_config.get("access_key_id", ""),
            aws_secret_access_key=central_s3_config.get("secret_access_key", ""),
            endpoint_url=central_s3_config.get("endpoint_url") or None,
        )
        central_bucket = central_s3_config.get("bucket", "")
        central_op = owner_params(central_s3_config)

    return (
        s3_config,
        central_s3_config,
        s3_client,
        bucket,
        s3_op,
        central_s3_client,
        central_bucket,
        central_op,
    )


def _qcow2_virtual_size_gb_s3(client, bucket, owner_params_dict, s3_path):
    """Read qcow2 virtual size from S3 header (Range request)."""
    try:
        if not client:
            return 0
        import struct

        resp = client.get_object(
            Bucket=bucket, Key=s3_path, Range="bytes=0-31", **owner_params_dict
        )
        header = resp["Body"].read()
        if len(header) >= 32 and header[:4] == b"QFI\xfb":
            vsize = struct.unpack(">Q", header[24:32])[0]
            return int(vsize / (1024**3)) + 1
    except Exception:
        pass
    return 0


def _check_central_source(
    s3_path, s3_client, bucket, s3_op, central_s3_client, central_bucket, central_op
):
    """Check if a disk exists in primary S3; if not, check central S3."""
    if not central_s3_client:
        return False
    try:
        s3_client.head_object(Bucket=bucket, Key=s3_path, **s3_op)
        return False
    except Exception:
        try:
            central_s3_client.head_object(
                Bucket=central_bucket, Key=s3_path, **central_op
            )
            return True
        except Exception:
            return False


def _resolve_pattern_disk(
    data, db, s3_client, bucket, s3_op, central_s3_client, central_bucket, central_op
):
    """Resolve S3 path for a pattern-sourced disk."""
    from app.models.pattern import PatternDisk as PatternDiskModel

    pid = data["patternId"]
    pattern_disk_id = data.get("patternDiskId", "")
    pd_record = (
        db.query(PatternDiskModel).filter_by(id=pattern_disk_id, pattern_id=pid).first()
        if pattern_disk_id
        else None
    )
    if pd_record and pd_record.s3_key:
        s3_path = pd_record.s3_key
    else:
        s3_path = f"patterns/{pid}/{pattern_disk_id}.qcow2"
    use_central = _check_central_source(
        s3_path, s3_client, bucket, s3_op, central_s3_client, central_bucket, central_op
    )
    data["resolvedS3Path"] = s3_path
    data["centralSource"] = use_central
    logger.info(
        "Deploy: disk %s s3=%s central=%s",
        data.get("label", "?"),
        s3_path[:40],
        use_central,
    )
    real_size = _qcow2_virtual_size_gb_s3(
        central_s3_client if use_central else s3_client,
        central_bucket if use_central else bucket,
        central_op if use_central else s3_op,
        s3_path,
    )
    if real_size and real_size > (data.get("size", 0) or 0):
        data["size"] = real_size


def _resolve_library_disk(
    data, db, s3_client, bucket, s3_op, central_s3_client, central_bucket, central_op
):
    """Resolve S3 path for a library-sourced disk."""
    from app.models.library import LibraryItem

    lib_item = db.get(LibraryItem, data["libraryItemId"])
    if lib_item and lib_item.s3_key:
        s3_path = lib_item.s3_key
        use_central = getattr(lib_item, "source", "") == "central"
    else:
        fmt = data.get("format", "qcow2")
        s3_path = f"library/{data['libraryItemId']}.{fmt}"
        use_central = _check_central_source(
            s3_path,
            s3_client,
            bucket,
            s3_op,
            central_s3_client,
            central_bucket,
            central_op,
        )
    data["resolvedS3Path"] = s3_path
    data["centralSource"] = use_central
    logger.info(
        "Deploy: disk %s s3=%s central=%s",
        data.get("label", "?"),
        s3_path[:40],
        use_central,
    )


def _resolve_disk_s3_paths(
    topology,
    db,
    s3_client,
    bucket,
    s3_op,
    central_s3_client,
    central_bucket,
    central_op,
):
    """Resolve S3 paths for pattern and library disks, annotating topology nodes."""
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") != "storageNode":
            continue
        if data.get("source") == "pattern" and data.get("patternId"):
            _resolve_pattern_disk(
                data,
                db,
                s3_client,
                bucket,
                s3_op,
                central_s3_client,
                central_bucket,
                central_op,
            )
        elif data.get("source") == "library" and data.get("libraryItemId"):
            _resolve_library_disk(
                data,
                db,
                s3_client,
                bucket,
                s3_op,
                central_s3_client,
                central_bucket,
                central_op,
            )


def _build_clone_name_map(topology):
    """Build a mapping from clone DV names to friendly disk labels."""
    clone_name_map: dict[str, str] = {}
    for node in topology.get("nodes", []):
        ndata = node.get("data", {})
        if node.get("type") != "storageNode":
            continue
        sid = ndata.get("id", node.get("id", ""))[:8]
        label = ndata.get("label", ndata.get("name", ""))
        fmt = ndata.get("format", "qcow2")
        node_id = ndata.get("id", node.get("id", ""))
        for edge in topology.get("edges", []):
            if edge.get("source") == node_id:
                vm_id = edge.get("target", "")[:8]
            elif edge.get("target") == node_id:
                vm_id = edge.get("source", "")[:8]
            else:
                continue
            clone_name_map[f"vm-{vm_id}-disk-{sid}"] = label
            if fmt == "iso":
                clone_name_map[f"vm-{vm_id}-cdrom"] = label
    return clone_name_map


def _format_import_progress(friendly, dv, dv_progress):
    """Format import-in-progress DataVolume status."""
    conditions = dv.get("status", {}).get("conditions", [])
    running_reason = ""
    running_msg = ""
    for cond in conditions:
        if cond.get("type") == "Running":
            running_reason = cond.get("reason", "")
            running_msg = cond.get("message", "")
            break
    if running_reason == "Completed":
        return f"{friendly}: writing to storage"
    if running_reason == "Error":
        return f"{friendly}: error — {running_msg[:40]}"
    if dv_progress and dv_progress != "N/A":
        try:
            pct = float(dv_progress.rstrip("%"))
            if pct >= 99.0:
                return f"{friendly}: writing to storage — please wait"
            return f"{friendly}: downloading {dv_progress}"
        except ValueError:
            return f"{friendly}: downloading {dv_progress}"
    if running_reason == "TransferRunning":
        return f"{friendly}: downloading starting"
    return f"{friendly}: starting"


def _format_dv_status_line(friendly, dv):
    """Format a single DataVolume into a human-readable status line."""
    dv_phase = dv.get("status", {}).get("phase", "")
    dv_progress = dv.get("status", {}).get("progress", "N/A")

    if dv_phase == "Succeeded":
        return f"{friendly}: done"
    if dv_phase == "ImportInProgress":
        return _format_import_progress(friendly, dv, dv_progress)
    if dv_phase in ("CloneInProgress", "CloneScheduled"):
        return f"{friendly}: cloning"
    if dv_phase in ("ImportScheduled", "Pending"):
        return f"{friendly}: scheduled"
    if dv_phase == "Failed":
        conditions = dv.get("status", {}).get("conditions", [])
        err = next(
            (
                c.get("message", "")
                for c in conditions
                if c.get("type") == "Running" and c.get("message")
            ),
            "",
        )
        short_err = err[:40] if err else "failed"
        return f"{friendly}: error — {short_err}"
    if dv_phase:
        return f"{friendly}: {dv_phase.lower()}"
    return f"{friendly}: waiting"


def _best_dv_status(lines):
    """Merge DV status lines keeping the most-advanced status per disk label."""
    rank = {"done": 5, "downloading": 4, "cloning": 3, "scheduled": 2, "waiting": 1}

    def _rank(s):
        for k, v in rank.items():
            if k in s:
                return v
        return 0

    best: dict[str, str] = {}
    for line in lines:
        label = line.split(":")[0].strip()
        dv_status = line.split(":", 1)[1].strip() if ":" in line else ""
        prev = best.get(label, "")
        if _rank(dv_status) >= _rank(prev):
            best[label] = dv_status
    return best


def _fill_missing_disk_labels(topology, best_status):
    """Add 'waiting' entries for topology disks not yet represented in DV status."""
    for node in topology.get("nodes", []):
        ndata = node.get("data", {})
        if node.get("type") == "storageNode" and ndata.get("source") in (
            "pattern",
            "library",
        ):
            label = ndata.get("label", ndata.get("name", ""))[:24]
            if label and label not in best_status:
                best_status[label] = "waiting"


def _collect_dv_progress(project_id, provider, topology):
    """Collect DataVolume progress lines for deploy status display."""
    import hashlib

    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    dv_lines: list[str] = []
    try:
        golden_name_map: dict[str, str] = {}
        for node in topology.get("nodes", []):
            ndata = node.get("data", {})
            if node.get("type") == "storageNode" and ndata.get("resolvedS3Path"):
                h = hashlib.sha256(ndata["resolvedS3Path"].encode()).hexdigest()[:16]
                golden_name_map[f"golden-{h}"] = ndata.get(
                    "label", ndata.get("name", "")
                )

        custom_api, _, _ = _get_k8s_clients(provider)
        proj_ns = _project_ns(provider, project_id)
        all_dvs: list = []
        for ns in ["troshka-cache", proj_ns]:
            try:
                dvs = custom_api.list_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="datavolumes",
                )
                all_dvs.extend(dvs.get("items", []))  # type: ignore[union-attr]
            except Exception:
                pass

        clone_name_map = _build_clone_name_map(topology)
        cache_lines: list[str] = []
        clone_lines: list[str] = []
        for dv in all_dvs:
            ns = dv["metadata"]["namespace"]
            raw_name = dv["metadata"]["name"]
            friendly = golden_name_map.get(raw_name) or clone_name_map.get(raw_name)
            if not friendly:
                continue
            line = _format_dv_status_line(friendly[:24], dv)
            if ns == "troshka-cache":
                cache_lines.append(line)
            else:
                clone_lines.append(line)

        best_status = _best_dv_status(cache_lines + clone_lines)
        _fill_missing_disk_labels(topology, best_status)
        dv_lines = [f"{k}: {v}" for k, v in best_status.items()]
    except Exception:
        pass
    return dv_lines


def _compute_deploy_step(project_id, status, dv_lines, progress):
    """Compute the current deploy step, detail text, and percent."""
    all_disks_done = dv_lines and all(": done" in line for line in dv_lines)
    op_stage = progress.get("stage", "") if progress else ""
    op_detail = progress.get("detail", "") if progress else ""
    dv_detail = "\n".join(dv_lines) if dv_lines else ""

    last = _get_deploy_progress_data(project_id) or {}
    step, detail = _resolve_deploy_step(
        all_disks_done, op_stage, op_detail, dv_detail, dv_lines, status, last
    )

    percent = progress.get("percent", 0) if progress else 0
    return step, detail, percent


def _resolve_deploy_step(
    all_disks_done, op_stage, op_detail, dv_detail, dv_lines, status, last
):
    """Determine step and detail from deploy state signals."""
    if all_disks_done and op_stage:
        step = op_stage.lower()
        if "certificate" in op_stage.lower():
            return step, op_detail or step
        vm_states = status.get("vmStates", {})
        if vm_states:
            ready = sum(1 for s in vm_states.values() if s in ("Running", "Stopped"))
            return step, f"{ready}/{len(vm_states)} VMs ready"
        return step, op_detail or step
    if dv_lines:
        return "images", dv_detail
    if op_stage:
        return op_stage.lower(), op_detail or op_stage.lower()
    return last.get("step", "") or "deploying", last.get("detail", "")


def _finalize_kubevirt_deploy(project_id, project, topology, db):
    """Handle the Running phase — update project state and topology."""
    from app.services.ws_pubsub import notify_project

    project.state = "active"
    clean_topo = copy.deepcopy(topology)
    for node in clean_topo.get("nodes", []):
        ndata = node.get("data", {})
        ndata.pop("resolvedS3Path", None)
        ndata.pop("presignedUrl", None)
        ndata.pop("ciGeneratedUserData", None)

    _allocate_kubevirt_eips(project_id, project, clean_topo, db)

    # Read domain UUIDs from TroshkaVM CRs (assigned by KubeVirt at VM creation)
    kv_domain_uuids = _read_kubevirt_domain_uuids(project, db)
    for node in clean_topo.get("nodes", []):
        ndata = node.get("data", {})
        if node.get("type") == "vmNode":
            vm_id = ndata.get("id", node.get("id", ""))
            if vm_id in kv_domain_uuids:
                ndata["domainUuid"] = kv_domain_uuids[vm_id]

    bmc_config = _extract_bmc_config(clean_topo, project_id)
    if bmc_config:
        clean_topo["bmc"] = {
            "username": bmc_config["bmc_network"].get("bmcUsername", "admin"),
            "password": bmc_config["bmc_network"].get("bmcPassword", "password"),
            "vms": {
                vm["node_id"]: {
                    "ip": vm["bmc_ip"],
                    "redfish_url": f"redfish-virtualmedia://{vm['bmc_ip']}:8000/redfish/v1/Systems/{kv_domain_uuids.get(vm['node_id'], 'troshka-vm-' + vm['node_id'][:8])}",
                    "redfish_url_ssl": f"redfish-virtualmedia+https://{vm['bmc_ip']}:8443/redfish/v1/Systems/{kv_domain_uuids.get(vm['node_id'], 'troshka-vm-' + vm['node_id'][:8])}",
                    "ipmi_address": f"{vm['bmc_ip']}:623",
                }
                for vm in bmc_config["vms"]
            },
        }
    project.deployed_topology = clean_topo
    project.topology = clean_topo
    project.deploy_error = None
    if _has_ocp_monitor(topology):
        project.ocp_status = "monitoring"
        project.ocp_status_detail = None
        project.ocp_install_elapsed = None
        project.ocp_monitor_started_at = datetime.datetime.now(datetime.UTC)
    project.deploy_progress = None
    db.commit()
    _delete_deploy_progress(project_id)
    notify_project(project_id, {"type": "project-state", "state": "active"})
    logger.info("Deploy %s: kubevirt deploy complete", project_id[:8])


def _allocate_kubevirt_eips(project_id, project, topology, db):
    """Allocate MetalLB EIPs for a kubevirt native project after operator deploy."""
    external_ips = topology.get("externalIps", [])
    if not external_ips:
        return

    from app.models.elastic_ip import ElasticIp
    from app.models.host import Host
    from app.models.provider import Provider
    from app.services.eip_service import allocate_eip, associate_eip
    from app.services.providers import get_provider_driver

    provider = (
        db.query(Provider).filter_by(id=project.provider_id).first()
        if project.provider_id
        else None
    )
    host = (
        db.query(Host).filter_by(id=project.host_id).first()
        if project.host_id
        else None
    )
    if not host and not provider:
        logger.warning("Deploy %s: no provider/host for EIP allocation", project_id[:8])
        return
    if not provider and host:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        logger.warning("Deploy %s: no provider for EIP allocation", project_id[:8])
        return

    driver = get_provider_driver(provider)
    logger.info(
        "Deploy %s: allocating %d MetalLB EIPs", project_id[:8], len(external_ips)
    )

    for ext_ip in external_ips:
        canvas_id = ext_ip.get("id", "")
        try:
            existing = (
                db.query(ElasticIp)
                .filter_by(project_id=project_id, canvas_eip_id=canvas_id)
                .first()
            )
            if existing:
                eip = existing
            else:
                eip = allocate_eip(db, provider, project_id, canvas_id, host)

            if eip.state != "associated":
                associate_eip(db, eip, host)

            ext_ip["ip"] = eip.public_ip

            pf_for_eip = []
            for node in topology.get("nodes", []):
                node_data = node.get("data", {})
                if node_data.get("subtype") == "gateway":
                    pf_for_eip = [
                        pf
                        for pf in node_data.get("portForwards", [])
                        if pf.get("extIpId") == canvas_id
                    ]
                    break
            if pf_for_eip:
                from app.services.providers.kubevirt import _project_ns

                ns = _project_ns(provider, project_id)
                driver.update_eip_ports(
                    provider,
                    host,
                    eip.allocation_id,
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
                "Deploy %s: EIP %s allocated (%s)",
                project_id[:8],
                canvas_id[:8],
                eip.public_ip,
            )
        except Exception:
            logger.exception(
                "Deploy %s: EIP allocation failed for %s (non-fatal)",
                project_id[:8],
                canvas_id[:8],
            )

    _patch_kubevirt_gateway_forwards(provider, project_id, topology)


def _read_kubevirt_domain_uuids(project, db):
    """Read domain UUIDs from TroshkaVM CR statuses (KubeVirt VM metadata.uid)."""
    from app.models.host import Host
    from app.models.provider import Provider
    from app.services.providers.kubevirt import (
        CRD_GROUP,
        CRD_VERSION,
        _get_k8s_clients,
        _project_ns,
    )

    host = (
        db.query(Host).filter_by(id=project.host_id).first()
        if project.host_id
        else None
    )
    if not host:
        return {}
    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return {}
    try:
        custom_api, _, _ = _get_k8s_clients(provider)
        ns = _project_ns(provider, project.id)
        vms = custom_api.list_namespaced_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, namespace=ns, plural="troshkavms"
        )
        result = {}
        for vm in dict(vms).get("items", []):  # type: ignore[call-overload]
            vm_id = vm.get("spec", {}).get("vmId", "")
            domain_uuid = vm.get("status", {}).get("domainUuid", "")
            if vm_id and domain_uuid:
                result[vm_id] = domain_uuid
        return result
    except Exception:
        logger.warning("Failed to read KubeVirt domain UUIDs for %s", project.id[:8])
        return {}


def _patch_kubevirt_gateway_forwards(provider, project_id, topology):
    """Patch the gateway Deployment with PORT_FORWARDS env var for DNAT rules."""
    all_forwards = []
    for node in topology.get("nodes", []):
        node_data = node.get("data", {})
        if node_data.get("subtype") == "gateway":
            for pf in node_data.get("portForwards", []):
                ext_port = pf.get("extPort", "")
                int_ip = pf.get("intIp", "")
                int_port = pf.get("intPort", "")
                if ext_port and int_ip and int_port:
                    proto = pf.get("proto", "tcp")
                    all_forwards.append(f"{ext_port}:{int_ip}:{int_port}:{proto}")
            break

    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    forwards_str = ",".join(all_forwards)
    try:
        _, _core_api, api_client = _get_k8s_clients(provider)
        from kubernetes import client as k8s_client

        apps_api = k8s_client.AppsV1Api(api_client)
        ns = _project_ns(provider, project_id)
        dep_name = f"gateway-{ns}"

        apps_api.patch_namespaced_deployment(
            name=dep_name,
            namespace=ns,
            body={
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "gateway",
                                    "env": [
                                        {
                                            "name": "PORT_FORWARDS",
                                            "value": forwards_str,
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                }
            },
        )
        logger.info(
            "Deploy %s: patched gateway with port forwards: %s",
            project_id[:8],
            forwards_str,
        )
    except Exception:
        logger.exception(
            "Deploy %s: failed to patch gateway port forwards (non-fatal)",
            project_id[:8],
        )


def _handle_kubevirt_deploy_error(project_id, project, status, db, notify_project):
    """Handle Error phase from kubevirt operator."""
    error_msg = (
        status.get("error") or status.get("message") or "Operator reported an error"
    )
    project.state = "error"
    project.deploy_error = error_msg
    db.commit()
    _delete_deploy_progress(project_id)
    notify_project(
        project_id,
        {"type": "project-state", "state": "error", "deploy_error": error_msg},
    )


def _push_kubevirt_deploy_progress(
    project_id, project, step, detail, percent, dv_lines, db, notify_project
):
    """Push deploy progress update if changed."""
    if not detail and not dv_lines:
        return
    last = _get_deploy_progress_data(project_id) or {}
    new_progress = {"step": step, "detail": detail, "percent": percent}
    if new_progress == last:
        return
    _set_deploy_progress(project_id, new_progress)
    project.deploy_progress = new_progress
    db.commit()
    notify_project(
        project_id,
        {
            "type": "deploy-progress",
            "step": step,
            "detail": detail,
            "percent": percent,
        },
    )


def _poll_kubevirt_deploy(project_id, project, provider, driver, topology, db):
    """Poll TroshkaProject CR status until Running/Error/timeout."""
    import time

    from app.services.ws_pubsub import notify_project

    _clear_deploy_cancelled(project_id)
    deploy_deadline = _time.time() + 7200
    for _ in range(1440):
        if _time.time() > deploy_deadline:
            break
        if _is_deploy_cancelled(project_id):
            logger.info("Deploy %s: cancelled by redeploy", project_id[:8])
            _clear_deploy_cancelled(project_id)
            return
        if _project_deleted(project_id):
            return
        try:
            status = driver.get_project_status(provider, project_id)
            if not isinstance(status, dict):
                status = {}
        except Exception:
            status = {}

        phase = status.get("phase", "Pending")
        progress = status.get("deployProgress", {})
        dv_lines = _collect_dv_progress(project_id, provider, topology)
        step, detail, percent = _compute_deploy_step(
            project_id, status, dv_lines, progress
        )

        if phase == "Running":
            _finalize_kubevirt_deploy(project_id, project, topology, db)
            return

        if phase == "Error":
            _handle_kubevirt_deploy_error(
                project_id, project, status, db, notify_project
            )
            return

        _push_kubevirt_deploy_progress(
            project_id, project, step, detail, percent, dv_lines, db, notify_project
        )

        time.sleep(5)

    # Timed out
    project.state = "error"
    project.deploy_error = "Deploy timed out waiting for operator (2 hours)"
    db.commit()
    _delete_deploy_progress(project_id)
    notify_project(
        project_id,
        {
            "type": "project-state",
            "state": "error",
            "deploy_error": project.deploy_error,
        },
    )


def _deploy_kubevirt_native(project_id, project, host, topology, db):
    """Deploy via KubeVirt operator — create TroshkaProject CR and poll status."""
    from app.models.provider import Provider
    from app.services.providers import get_provider_driver
    from app.services.ws_pubsub import notify_project

    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        project.state = "error"
        project.deploy_error = "No provider found for kubevirt host"
        db.commit()
        return

    driver = get_provider_driver(provider)

    (
        s3_config,
        central_s3_config,
        s3_client,
        bucket,
        s3_op,
        central_s3_client,
        central_bucket,
        central_op,
    ) = _setup_kubevirt_s3_clients()

    _resolve_disk_s3_paths(
        topology,
        db,
        s3_client,
        bucket,
        s3_op,
        central_s3_client,
        central_bucket,
        central_op,
    )

    # Generate per-deploy SSH key pair for exec pod → VM access
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    logger.info("Deploy %s: generating exec SSH key pair", project_id[:8])
    exec_key = Ed25519PrivateKey.generate()
    exec_privkey_pem = exec_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    exec_pubkey = (
        exec_key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        .decode()
        + " troshka-exec"
    )

    # Regenerate cloud-init userdata so deploy-time settings (guest-exec, etc.) take effect
    from app.services.cloud_init import generate_userdata

    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if not data.get("cloudInit"):
            continue
        if not project.guest_exec_enabled:
            data["guestExecEnabled"] = False
        ssh_keys = [k for k in data.get("ciSshKeys", []) if "troshka-exec" not in k]
        ssh_keys.append(exec_pubkey)
        data["ciSshKeys"] = ssh_keys
        data["ciGeneratedUserData"] = generate_userdata(data)

    _update_deploy_progress(project_id, "networks", "creating operator resources")
    notify_project(
        project_id,
        {
            "type": "deploy-progress",
            "step": "networks",
            "detail": "creating operator resources",
        },
    )

    # Wait for namespace to finish terminating (race with destroy)
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    _kc_api, _kc_core, _ = _get_k8s_clients(provider)
    _kc_ns = _project_ns(provider, project_id)
    for _ns_wait in range(60):
        try:
            ns_obj = _kc_core.read_namespace(name=_kc_ns)
            if ns_obj.status.phase == "Terminating":  # type: ignore[union-attr]
                if _ns_wait == 0:
                    logger.info(
                        "Deploy %s: waiting for namespace to finish terminating",
                        project_id[:8],
                    )
                _time.sleep(3)
                continue
            break
        except Exception:
            break

    existing_cr = None
    cr_name = f"project-{project_id[:8]}"
    try:
        existing_cr = driver.get_project_status(provider, project_id)
    except Exception:
        pass

    _resume_poll = False
    if existing_cr and existing_cr.get("phase") == "Deploying":
        cr_name = f"project-{project_id[:8]}"
        _resume_poll = True
        logger.info("Deploy %s: CR already deploying, resuming poll", project_id[:8])
    elif existing_cr and existing_cr.get("phase"):
        logger.info(
            "Deploy %s: replacing stale CR with fresh presigned URLs",
            project_id[:8],
        )
        try:
            from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

            custom_api, _, _ = _get_k8s_clients(provider)
            ns = _project_ns(provider, project_id)
            custom_api.delete_namespaced_custom_object(
                group="troshka.redhat.com",
                version="v1alpha1",
                namespace=ns,
                plural="troshkaprojects",
                name=f"project-{project_id[:8]}",
            )
        except Exception:
            pass

        # Wait for old CR and resources to be fully gone before creating new CR
        from app.services.providers.kubevirt import (
            _get_k8s_clients as _kc,
        )
        from app.services.providers.kubevirt import (
            _project_ns as _pns,
        )

        _ca, _cv1, _ac = _kc(provider)
        _ns = _pns(provider, project_id)
        cr_name = f"project-{project_id[:8]}"

        # Delete any stale Jobs
        try:
            from kubernetes import client as _klient

            _batch = _klient.BatchV1Api(_ac)
            for job in _batch.list_namespaced_job(_ns).items:  # type: ignore[union-attr]
                try:
                    _batch.delete_namespaced_job(
                        name=job.metadata.name,
                        namespace=_ns,
                        propagation_policy="Background",
                    )
                except Exception:
                    pass
        except Exception:
            pass

        # Wait for CR + VMIs + pods to be fully gone
        for attempt in range(45):
            try:
                cr_gone = True
                try:
                    _ca.get_namespaced_custom_object(
                        group="troshka.redhat.com",
                        version="v1alpha1",
                        namespace=_ns,
                        plural="troshkaprojects",
                        name=cr_name,
                    )
                    cr_gone = False
                except Exception:
                    pass

                _vmis = _ca.list_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=_ns,
                    plural="virtualmachineinstances",
                )
                _pods = _cv1.list_namespaced_pod(
                    _ns, label_selector="kubevirt.io=virt-launcher"
                )
                if cr_gone and not _vmis.get("items", []) and not _pods.items:  # type: ignore[union-attr]
                    logger.info(
                        "Deploy %s: old resources cleaned up after %ds",
                        project_id[:8],
                        attempt * 2,
                    )
                    break
            except Exception:
                break
            _time.sleep(2)
        else:
            logger.warning(
                "Deploy %s: old resources still present after 90s, proceeding",
                project_id[:8],
            )

    if not _resume_poll:
        logger.info(
            "Deploy %s: creating CR with exec_ssh_key=%s",
            project_id[:8],
            bool(exec_privkey_pem),
        )
        try:
            logger.info(
                "Deploy %s: central_s3=%s",
                project_id[:8],
                bool(central_s3_config),
            )
            cr_name = driver.deploy_project(
                provider,
                project_id,
                topology,
                s3_config,
                exec_ssh_key=exec_privkey_pem,
                central_s3_config=central_s3_config,
            )
        except Exception as e:
            if "AlreadyExists" in str(e):
                cr_name = f"project-{project_id[:8]}"
                logger.info("Deploy %s: CR already exists, resuming", project_id[:8])
            else:
                project.state = "error"
                project.deploy_error = f"Failed to create TroshkaProject CR: {e}"
                db.commit()
                notify_project(
                    project_id,
                    {
                        "type": "project-state",
                        "state": "error",
                        "deploy_error": project.deploy_error,
                    },
                )
                return

    logger.info(
        "Deploy %s: polling TroshkaProject CR %s",
        project_id[:8],
        cr_name,
    )

    _poll_kubevirt_deploy(project_id, project, provider, driver, topology, db)


def deploy_project_async(  # pyright: ignore[reportGeneralTypeIssues]
    project_id: str, auto_start: bool = True, resume_from: str | None = None
):
    """Background thread: deploy a project's topology to a host."""
    acquired = _deploy_semaphore.acquire(timeout=1800)
    if not acquired:
        from app.core.database import SessionLocal
        from app.models.project import Project

        s = SessionLocal()
        try:
            p = s.get(Project, project_id)
            if p:
                p.state = "error"
                p.deploy_error = "Too many concurrent deploys — try again shortly"
                s.commit()
                notify_project(project_id, {"type": "project-state", "state": "error"})
        finally:
            s.close()
        return
    try:
        _deploy_project_inner(project_id, auto_start, resume_from)
    finally:
        _deploy_semaphore.release()


def _deploy_project_inner(  # pyright: ignore[reportGeneralTypeIssues]
    project_id: str, auto_start: bool = True, resume_from: str | None = None
):
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.services.placement import record_deploy_end, record_deploy_start

    # Clear cancellation flag for this deploy
    _clear_deploy_cancelled(project_id)

    _host_id_for_inflight: str | None = None
    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "deploying":
            return
        if resume_from:
            logger.info(
                "Deploy %s: resuming from step '%s'", project_id[:8], resume_from
            )

        host = (
            s.query(Host).filter_by(id=project.host_id).first()
            if project.host_id
            else None
        )
        if not host and not project.host_id:
            from app.services.placement import (
                calculate_project_requirements,
                find_available_host,
            )

            reqs = calculate_project_requirements(project.topology or {})
            host = find_available_host(s, reqs["total_vcpus"], reqs["total_ram_mb"])
            if host:
                project.host_id = host.id
                s.commit()
                logger.info(
                    "Deploy %s: auto-placed on host %s", project_id[:8], host.id[:8]
                )
        if host:
            _host_id_for_inflight = host.id
            record_deploy_start(host.id)

        if not host or not host.ip_address:
            if not project.host_id:
                from app.services.placement import (
                    calculate_project_requirements as _calc_reqs,
                )

                reqs = _calc_reqs(project.topology or {})
                ram_gb = round(reqs["total_ram_mb"] / 1024, 1)
                error_msg = f"Not enough capacity in pool — need {reqs['total_vcpus']} vCPUs and {ram_gb} GB RAM but no host has room. Free up resources or add a host."
            elif not host:
                error_msg = "Assigned host no longer exists"
            else:
                error_msg = (
                    "Assigned host has no IP address — it may still be provisioning"
                )
            project.state = "error"
            project.deploy_error = error_msg
            s.commit()
            notify_project(
                project_id,
                {
                    "type": "project-state",
                    "state": "error",
                    "deploy_error": error_msg,
                },
            )
            return

        topology = project.topology or {}
        clock_offset = None
        if project.clock_target:
            from app.services.clock_service import compute_clock_offset

            clock_offset = compute_clock_offset(project.clock_target)
        vni_map = project.vni_map or {}
        if not vni_map:
            from app.services.vxlan import allocate_vnis_for_project

            vni_map = allocate_vnis_for_project(s, topology)
            project.vni_map = vni_map
            s.commit()
            logger.info("Deploy %s: allocated VNIs %s", project_id[:8], vni_map)

        # KubeVirt native: delegate entire deploy to operator via CRDs
        if host.host_type == "kubevirt-cluster":
            _deploy_kubevirt_native(project_id, project, host, topology, s)
            return

        pool = _get_host_pool(host, s)
        disk_cache = "none" if pool and pool.mode.startswith("shared") else None

        # Step 0: Allocate and associate EIPs (before networking so DNAT rules have private IPs)
        external_ips = topology.get("externalIps", [])
        if external_ips and not _should_skip(resume_from, "eips"):
            _checkpoint(s, project_id, "eips")
            _update_deploy_progress(project_id, "eips", "allocating elastic IPs")
            logger.info(
                "Deploy %s: allocating %d EIPs", project_id[:8], len(external_ips)
            )
            from app.models.elastic_ip import ElasticIp
            from app.models.provider import Provider
            from app.services.eip_service import (
                allocate_eip,
                allocate_transit_ports,
                associate_eip,
                sync_security_group_rules,
            )
            from app.services.providers import get_provider_driver

            provider = (
                s.query(Provider).filter_by(id=project.provider_id).first()
                if project.provider_id
                else None
            )
            if not provider and host.provider_id:
                provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if not provider:
                project.state = "error"
                project.deploy_error = "No provider configured for EIP allocation"
                s.commit()
                _delete_deploy_progress(project_id)
                return

            for ext_ip in external_ips:
                canvas_id = ext_ip.get("id", "")

                # OCP Virt: skip EIP allocation when all port forwards are
                # routable via OCP Routes (443/80) — Routes replace EIPs
                if provider.type == "ocpvirt":
                    pf_ports = set()
                    for node in topology.get("nodes", []):
                        node_data = node.get("data", {})
                        if node_data.get("subtype") == "gateway":
                            for pf in node_data.get("portForwards", []):
                                if pf.get("extIpId") == canvas_id:
                                    pf_ports.add(int(pf.get("extPort", 0)))
                            break
                    if pf_ports and pf_ports.issubset({80, 443}):
                        logger.info(
                            "Deploy %s: skipping EIP for %s — all ports (%s) handled by Routes",
                            project_id[:8],
                            canvas_id[:8],
                            pf_ports,
                        )
                        ext_ip["_skip"] = True
                        continue

                existing = (
                    s.query(ElasticIp)
                    .filter_by(project_id=project_id, canvas_eip_id=canvas_id)
                    .first()
                )
                if existing:
                    eip = existing
                else:
                    eip = allocate_eip(s, provider, project_id, canvas_id, host)

                if eip.state != "associated":
                    associate_eip(s, eip, host)

                ext_ip["ip"] = eip.public_ip
                ext_ip["_private_ip"] = eip.private_ip

                if provider.type != "ec2" and not eip.port_map:
                    pf_for_eip = []
                    for node in topology.get("nodes", []):
                        node_data = node.get("data", {})
                        if node_data.get("subtype") == "gateway":
                            pf_for_eip = [
                                pf
                                for pf in node_data.get("portForwards", [])
                                if pf.get("extIpId") == canvas_id
                            ]
                            break
                    if pf_for_eip:
                        port_map = allocate_transit_ports(s, eip, host, pf_for_eip)
                        driver = get_provider_driver(provider)
                        driver.update_eip_ports(
                            provider,
                            host,
                            eip.allocation_id,
                            [
                                {
                                    "port": int(ep),
                                    "targetPort": tp,
                                    "name": f"pf-{i}",
                                }
                                for i, (ep, tp) in enumerate(port_map.items())
                            ],
                        )

                if eip.port_map:
                    ext_ip["_transit_port_map"] = eip.port_map

            # Clean up internal markers (keep EIP entries so port forward
            # references remain valid — OCP Virt EIPs just have no allocated IP)
            for ext_ip in external_ips:
                ext_ip.pop("_skip", None)

            project.topology = topology
            s.commit()

        # Auto-assign IPs to container NICs without static IPs (before network setup
        # so dnsmasq gets static host entries for containers)
        _auto_assign_container_ips(topology)

        # Step 1: Set up VXLAN networks (serialized to avoid nftables contention)
        _checkpoint(s, project_id, "networks")
        _update_deploy_progress(project_id, "networking", "waiting for lock")
        with _get_network_lock(host.id):
            _update_deploy_progress(project_id, "networking", "configuring VXLAN")
            logger.info(
                "Deploy %s: setting up networks on %s", project_id[:8], host.ip_address
            )

            net_result = _setup_networks_via_troshkad(
                host, topology, vni_map, s, project_id
            )
        if net_result is not True:
            logger.error("Deploy %s: %s", project_id[:8], net_result)
            project.state = "error"
            project.deploy_error = net_result
            s.commit()
            _delete_deploy_progress(project_id)
            return

        # Step 1a: Set up load balancer (HAProxy) if present
        from app.services.vxlan import build_host_network_config as _build_net_config

        _net_config = _build_net_config(topology, vni_map, [])
        lb_config = _net_config.get("loadbalancer")
        if lb_config and lb_config.get("frontends"):
            _update_deploy_progress(project_id, "load balancer", "starting HAProxy")
            logger.info("Deploy %s: setting up load balancer", project_id[:8])
            ns = f"troshka-{project_id[:8]}"
            # Default LB IP to gateway+1 if not set
            lb_ip = lb_config.get("lb_ip", "")
            if not lb_ip:
                net_list = _net_config.get("networks", [])
                if net_list:
                    import ipaddress as _ipa

                    first_cidr = net_list[0].get("dhcp_config", {}).get("gateway", "")
                    if first_cidr:
                        try:
                            lb_ip = str(_ipa.IPv4Address(first_cidr) + 1)
                        except (ValueError, _ipa.AddressValueError):
                            pass
            lb_params = {
                "ns": ns,
                "project_id": project_id,
                "frontends": lb_config["frontends"],
                "backends": lb_config["backends"],
                "lb_ip": lb_ip,
            }
            try:
                lb_job = start_job(host, "/lb/setup", lb_params)
                wait_for_job(host, lb_job, timeout=30)
            except TroshkadError as e:
                logger.warning("Deploy %s: LB setup failed: %s", project_id[:8], e)

        # Step 1b: Sync SG rules for port forwards (gateway + LB)
        if external_ips:
            from app.models.provider import Provider as _Prov
            from app.services.eip_service import sync_security_group_rules

            _provider = (
                s.query(_Prov).filter_by(id=project.provider_id).first()
                if project.provider_id
                else None
            )
            if not _provider and host.provider_id:
                _provider = s.query(_Prov).filter_by(id=host.provider_id).first()
            if _provider:
                desired_sg = []
                gateway_node = next(
                    (
                        n
                        for n in topology.get("nodes", [])
                        if n.get("type") == "networkNode"
                        and n.get("data", {}).get("subtype") == "gateway"
                    ),
                    None,
                )
                if (
                    gateway_node
                    and gateway_node.get("data", {}).get("gatewayMode")
                    == "nat-portforward"
                ):
                    for pf in gateway_node.get("data", {}).get("portForwards", []):
                        if pf.get("extPort"):
                            desired_sg.append(
                                {
                                    "project_id": project_id,
                                    "ext_port": int(pf["extPort"]),
                                    "protocol": "tcp",
                                }
                            )
                if (
                    lb_config
                    and lb_config.get("frontends")
                    and lb_config.get("external", True)
                ):
                    for fe in lb_config["frontends"]:
                        desired_sg.append(
                            {
                                "project_id": project_id,
                                "ext_port": int(fe["bindPort"]),
                                "protocol": "tcp",
                            }
                        )
                if desired_sg:
                    sync_security_group_rules(s, _provider, desired_sg)

        if _project_deleted(project_id):
            logger.info(
                "Deploy %s: project deleted mid-deploy, aborting", project_id[:8]
            )
            _delete_deploy_progress(project_id)
            return

        # Step 1c: Inject gateway IP for NTP into VM data (before seed ISOs)
        gateway_ip = None
        for node in topology.get("nodes", []):
            if node.get("type") == "gatewayNode":
                for edge in topology.get("edges", []):
                    if edge.get("source") == node["id"]:
                        target_node = next(
                            (n for n in topology["nodes"] if n["id"] == edge["target"]),
                            None,
                        )
                        if target_node and target_node.get("type") == "networkNode":
                            net_data = target_node.get("data", {})
                            cidr = net_data.get("cidr", "192.168.1.0/24")
                            import ipaddress

                            network = ipaddress.ip_network(cidr, strict=False)
                            gateway_ip = str(network.network_address + 1)
                            break
                break

        if gateway_ip:
            for node in topology.get("nodes", []):
                if node.get("type") == "vmNode" and node.get("data", {}).get(
                    "cloudInit"
                ):
                    node["data"]["gateway_ip"] = gateway_ip
            logger.info(
                "Deploy %s: injected gateway_ip %s into VM cloud-init data",
                project_id[:8],
                gateway_ip,
            )

        if not project.guest_exec_enabled:
            for node in topology.get("nodes", []):
                if node.get("type") == "vmNode" and node.get("data", {}).get(
                    "cloudInit"
                ):
                    node["data"]["guestExecEnabled"] = False

        # Create Route-based access for OCP Virt port forwards on 443/80
        # Runs after network setup so nftables chains exist for DNAT rules
        if host and host.provider_id:
            from app.models.provider import Provider
            from app.services.providers import get_provider_driver

            provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if provider and provider.type == "ocpvirt":
                driver = get_provider_driver(provider)
                external_endpoints = []
                for node in topology.get("nodes", []):
                    node_data = node.get("data", {})
                    if node_data.get("subtype") != "gateway":
                        continue
                    for pf in node_data.get("portForwards", []):
                        ext_port = int(pf.get("extPort", 0))
                        if ext_port not in (80, 443, 6443):
                            continue
                        int_ip = pf.get("intIp", "")
                        int_port = int(pf.get("intPort", ext_port))
                        vm_name = _find_vm_name_by_ip(topology, int_ip)
                        try:
                            result = driver.create_route_access(
                                provider,
                                host,
                                project_id,
                                vm_name,
                                int_ip,
                                ext_port,
                                int_port,
                            )
                            external_endpoints.append(
                                {
                                    "vmName": vm_name,
                                    "vmIp": int_ip,
                                    "port": ext_port,
                                    "type": "route",
                                    "hostname": result["hostname"],
                                }
                            )
                            logger.info(
                                "Deploy %s: created Route for %s:%d → %s",
                                project_id[:8],
                                vm_name,
                                ext_port,
                                result["hostname"],
                            )
                        except Exception:
                            logger.warning(
                                "Deploy %s: Route creation failed for %s:%d, continuing",
                                project_id[:8],
                                vm_name,
                                ext_port,
                                exc_info=True,
                            )
                    if external_endpoints:
                        node_data["externalEndpoints"] = external_endpoints
                    break

                project.topology = topology
                s.commit()

        # Step 2: Create cloud-init seed ISOs
        _checkpoint(s, project_id, "seeds")
        _update_deploy_progress(project_id, "cloud-init", "creating seed ISOs")
        logger.info("Deploy %s: creating cloud-init seed ISOs", project_id[:8])
        _create_seed_isos_via_troshkad(host, project_id, topology, pool)

        # Step 2b: Deploy metadata service
        _update_deploy_progress(project_id, "cloud-init", "deploying metadata service")
        logger.info("Deploy %s: deploying metadata service", project_id[:8])
        _setup_metadata_via_troshkad(host, project_id, topology, vni_map)

        if _project_deleted(project_id):
            logger.info(
                "Deploy %s: project deleted mid-deploy, aborting", project_id[:8]
            )
            _delete_deploy_progress(project_id)
            return

        # Step 3: Cache library images on host
        _checkpoint(s, project_id, "images")
        _update_deploy_progress(project_id, "downloading images", "0%")
        logger.info("Deploy %s: caching library images", project_id[:8])

        def _deploy_dl_progress(detail, items):
            _update_deploy_progress(
                project_id, "downloading images", str(detail), items=items
            )

        cache_library_images(topology, host, s, progress_callback=_deploy_dl_progress)

        # Step 3b: Set up PXE boot services (extract kernel/initrd, start HTTP server)
        logger.info("Deploy %s: setting up PXE boot services", project_id[:8])
        _setup_pxe_via_troshkad(host, topology, vni_map, project_id)

        # Step 3c: Pull container images
        _checkpoint(s, project_id, "container_pull")
        containers = _extract_containers(topology)
        logger.info(
            "Deploy %s: found %d containers to pull", project_id[:8], len(containers)
        )
        if containers:
            is_pattern_deploy = _is_pattern_deploy(topology)
            pattern_id = None
            if is_pattern_deploy:
                # Extract pattern_id from any storage node
                for node in topology.get("nodes", []):
                    if node.get("type") == "storageNode":
                        pattern_id = node.get("data", {}).get("patternId")
                        if pattern_id:
                            break

            _update_deploy_progress(
                project_id, step="container_pull", detail="Pulling container images..."
            )
            logger.info("Deploy %s: pulling container images", project_id[:8])
            for ctr in containers:
                if ctr.get("is_pod"):
                    all_images = set()
                    for ic in ctr.get("init_containers", []):
                        if ic.get("image"):
                            all_images.add(ic["image"])
                    for pc in ctr.get("pod_containers", []):
                        if pc.get("image"):
                            all_images.add(pc["image"])
                    for img in all_images:
                        pull_params = {"image": img}
                        cred_id = ctr.get("registry_credential_id")
                        if cred_id:
                            from app.core.encryption import decrypt
                            from app.models.registry_credential import (
                                RegistryCredential,
                            )

                            cred = (
                                s.query(RegistryCredential)
                                .filter_by(id=cred_id)
                                .first()
                            )
                            if cred:
                                pull_params["registry"] = cred.registry_url
                                pull_params["username"] = cred.username
                                pull_params["password"] = decrypt(cred.password)
                        job_id = start_job(host, "/containers/pull", pull_params)
                        wait_for_job(host, job_id, timeout=600)
                    continue

                if not ctr["image"]:
                    continue

                if is_pattern_deploy and pattern_id:
                    # Load from pattern cache instead of pulling
                    tar_filename = f"container-{ctr['node_id'][:8]}-image.tar.gz"
                    cache_path = f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{tar_filename}"
                    s3_key = f"patterns/{pattern_id}/{tar_filename}"

                    from app.services.s3_storage import _bucket, _get_s3_config

                    creds = _get_s3_config()

                    # Download from S3 if not cached
                    logger.info(
                        "Deploy %s: downloading container image %s from pattern cache",
                        project_id[:8],
                        ctr["image"],
                    )
                    job_id = start_job(
                        host,
                        "/images/cache",
                        {
                            "url": f"s3://{_bucket()}/{s3_key}",
                            "cache_path": cache_path,
                            "aws_access_key_id": creds.get("access_key_id", ""),
                            "aws_secret_access_key": creds.get("secret_access_key", ""),
                            "aws_region": creds.get("region", "us-east-1"),
                            "aws_endpoint_url": creds.get("endpoint_url", ""),
                        },
                    )
                    wait_for_job(host, job_id, timeout=600)

                    # Load image from tar.gz
                    logger.info(
                        "Deploy %s: loading container image %s from cache",
                        project_id[:8],
                        ctr["image"],
                    )
                    job_id = start_job(
                        host, "/containers/load-image", {"input_path": cache_path}
                    )
                    wait_for_job(host, job_id, timeout=300)
                else:
                    # Normal pull from registry
                    pull_params = {"image": ctr["image"]}

                    # Resolve registry credentials
                    cred_id = ctr.get("registry_credential_id")
                    if cred_id:
                        from app.core.encryption import decrypt
                        from app.models.registry_credential import RegistryCredential

                        cred = s.query(RegistryCredential).filter_by(id=cred_id).first()
                        if cred:
                            pull_params["registry"] = cred.registry_url
                            pull_params["username"] = cred.username
                            pull_params["password"] = decrypt(cred.password)

                    job_id = start_job(host, "/containers/pull", pull_params)
                    wait_for_job(host, job_id, timeout=600)

        if _project_deleted(project_id):
            logger.info(
                "Deploy %s: project deleted mid-deploy, aborting", project_id[:8]
            )
            _delete_deploy_progress(project_id)
            return

        # Step 3d: Validate BMC configuration
        bmc_network_exists = any(
            n.get("type") == "networkNode"
            and n.get("data", {}).get("networkType") == "bmc"
            for n in topology.get("nodes", [])
        )
        if bmc_network_exists:
            missing_bmc_ips = [
                n["data"].get("name", n["id"][:8])
                for n in topology.get("nodes", [])
                if n.get("type") == "vmNode"
                and n.get("data", {}).get("bmcEnabled")
                and not n.get("data", {}).get("bmcIp")
            ]
            if missing_bmc_ips:
                error_msg = (
                    f"BMC-enabled VMs missing BMC IP: {', '.join(missing_bmc_ips)}"
                )
                logger.error("Deploy %s: %s", project_id[:8], error_msg)
                project.state = "error"
                project.deploy_error = error_msg
                s.commit()
                notify_project(
                    project_id,
                    {
                        "type": "project-state",
                        "state": "error",
                        "deploy_error": error_msg,
                    },
                )
                _delete_deploy_progress(project_id)
                return

        # Create BMC bridge (before VMs so libvirt can validate the bridge name)
        bmc_config = _extract_bmc_config(topology, project_id)
        if bmc_config:
            from app.services.troshkad_client import (
                start_job as _sj,
            )
            from app.services.troshkad_client import (
                wait_for_job as _wj,
            )

            net_data = bmc_config["bmc_network"]
            cidr = net_data.get("cidr", "192.168.100.0/24")
            _bj = _sj(
                host,
                "/bmc/create-bridge",
                {
                    "project_id": project_id,
                    "bmc_cidr": cidr,
                    "bmc_gateway_ip": cidr.rsplit(".", 1)[0] + ".1",
                    "vms": [{"bmc_ip": vm["bmc_ip"]} for vm in bmc_config["vms"]],
                },
            )
            _wj(host, _bj, timeout=30)
            logger.info("Deploy %s: BMC bridge created", project_id[:8])

        if _project_deleted(project_id):
            logger.info(
                "Deploy %s: project deleted mid-deploy, aborting", project_id[:8]
            )
            _delete_deploy_progress(project_id)
            return

        # Step 4: Create VM disks and definitions (parallel)
        _checkpoint(s, project_id, "disks")
        _update_deploy_progress(project_id, "creating", "VMs")
        logger.info("Deploy %s: creating VMs", project_id[:8])
        vms = _extract_vms(topology)

        # Fire all disk creation jobs in parallel (VMs + container volumes)
        _update_deploy_progress(project_id, "creating disks", "preparing VM disks")
        disk_jobs = []
        for vm in vms:
            vm_disks = _find_vm_disks(vm["node_id"], topology)
            job_ids = _create_vm_disks_via_troshkad(
                host, project_id, vm, vm_disks, pool
            )
            disk_jobs.extend(job_ids if isinstance(job_ids, list) else [])

        # Create raw volumes for containers
        containers = _extract_containers(topology)
        for ctr in containers:
            ctr_vols = _find_container_volumes(
                ctr["node_id"], topology, project_id, pool
            )
            for vol in ctr_vols:
                jid = start_job(
                    host,
                    "/disks/create",
                    {
                        "path": vol["disk_path"],
                        "size_gb": vol["size_gb"],
                        "format": "raw",
                    },
                )
                disk_jobs.append(jid)
        for di, jid in enumerate(disk_jobs):
            try:
                _update_deploy_progress(
                    project_id, "creating disks", f"{di}/{len(disk_jobs)}"
                )
                job = wait_for_job(host, jid, timeout=900)
                if job.get("status") == "failed":
                    raise TroshkadError(
                        f"Disk creation failed: {job.get('result', {}).get('error', 'unknown')}"
                    )
            except TroshkadError as e:
                logger.error("Deploy %s: disk creation failed: %s", project_id[:8], e)
                raise

        # Step 4a: Recert RHCOS disks (must happen before virt-install locks the disks)
        if _is_pattern_deploy(topology) and _is_ocp_topology(topology):
            _update_deploy_progress(project_id, "certs", "regenerating certificates")
            deploy_recert = topology.pop("_deploy_recert", None)
            common_password = topology.pop("_deploy_common_password", None)
            if deploy_recert is None:
                for node in topology.get("nodes", []):
                    if node.get("type") == "storageNode":
                        pid = node.get("data", {}).get("patternId")
                        if pid:
                            pat = s.query(Pattern).filter_by(id=pid).first()
                            if pat and pat.recert:
                                deploy_recert = True
                            break
            if not common_password:
                for n in topology.get("nodes", []):
                    if n.get("type") == "vmNode" and n.get("data", {}).get("cloudInit"):
                        common_password = n.get("data", {}).get("ciCloudUserPassword")
                        if common_password:
                            break
            if deploy_recert and deploy_recert is not False:
                has_recert_vm = any(
                    n.get("type") == "vmNode" and n.get("data", {}).get("recertEnabled")
                    for n in topology.get("nodes", [])
                )
                if not has_recert_vm:
                    for n in topology.get("nodes", []):
                        if (
                            n.get("type") == "vmNode"
                            and n.get("data", {}).get("os") == "rhcos"
                        ):
                            n.setdefault("data", {})["recertEnabled"] = True
                    logger.info(
                        "Deploy %s: auto-enabled recert on RHCOS VMs from pattern",
                        project_id[:8],
                    )
            if deploy_recert is False:
                logger.info(
                    "Deploy %s: recert disabled by user, using guestfish",
                    project_id[:8],
                )
            _clean_kubelet_certs(
                host,
                project_id,
                topology,
                pool,
                pattern_recert=bool(deploy_recert),
                common_password=common_password,
            )

        # Create VM definitions sequentially (virt-install storage pool race condition)
        _checkpoint(s, project_id, "vms")
        for vi, vm in enumerate(vms):
            vm_name = vm.get("name", vm["node_id"][:8])
            items = []
            for vj, v in enumerate(vms):
                n = v.get("name", v["node_id"][:8])
                if vj < vi:
                    items.append(f"{n}: defined")
                elif vj == vi:
                    items.append(f"{n}: defining...")
                else:
                    items.append(f"{n}: pending")
            _update_deploy_progress(
                project_id, "creating VMs", f"{vi}/{len(vms)}", items=items
            )
            domain_name = f"troshka-{project_id[:8]}-{vm['node_id'][:8]}"
            try:
                dom_check = start_job(host, "/vm/info", {"name": domain_name})
                dom_result = wait_for_job(host, dom_check, timeout=10)
                if dom_result.get("result", {}).get("state"):
                    logger.info(
                        "Deploy %s: stale domain %s exists, undefining before re-create",
                        project_id[:8],
                        domain_name,
                    )
                    try:
                        j = start_job(
                            host, "/vms/destroy", {"domain_name": domain_name}
                        )
                        wait_for_job(host, j, timeout=60)
                    except TroshkadError:
                        pass
            except TroshkadError:
                pass

            job_id = _create_vm_via_troshkad(
                host, project_id, vm, topology, vni_map, pool, disk_cache, clock_offset
            )
            if job_id:
                try:
                    job = wait_for_job(host, job_id, timeout=300)
                    if job.get("status") == "failed":
                        raise TroshkadError(
                            f"VM definition failed: {job.get('result', {}).get('error', 'unknown')}"
                        )
                    dom_uuid = job.get("result", {}).get("domain_uuid", "")
                    if dom_uuid:
                        for n in topology.get("nodes", []):
                            if n["id"] == vm["node_id"]:
                                n.setdefault("data", {})["domainUuid"] = dom_uuid
                                break
                except TroshkadError as e:
                    logger.error("Deploy %s: VM creation failed: %s", project_id[:8], e)
                    raise

        # Persist domain UUIDs to topology
        project.topology = topology
        s.commit()

        # Step 4b: Start BMC endpoints (after VMs are defined, before startup)
        has_bmc_vms = any(
            n.get("type") == "vmNode" and n.get("data", {}).get("bmcEnabled")
            for n in topology.get("nodes", [])
        )
        bmc_config = _extract_bmc_config(topology, project_id)
        if has_bmc_vms and not bmc_config:
            error_msg = "VMs have BMC enabled but no BMC network (type: bmc) is defined"
            logger.error("Deploy %s: %s", project_id[:8], error_msg)
            project.state = "error"
            project.deploy_error = error_msg
            s.commit()
            _delete_deploy_progress(project_id)
            return
        if bmc_config:
            _update_deploy_progress(project_id, "bmc", "starting BMC endpoints")
            notify_project(
                project_id,
                {
                    "type": "deploy-progress",
                    "progress": _get_deploy_progress_data(project_id) or {},
                },
            )
            logger.info(
                "Deploy %s: starting BMC endpoints for %d VMs",
                project_id[:8],
                len(bmc_config["vms"]),
            )
            bmc_result = _setup_bmc_via_troshkad(host, project_id, bmc_config)
            if bmc_result is not True:
                logger.error(
                    "Deploy %s: BMC setup failed: %s", project_id[:8], bmc_result
                )
                project.state = "error"
                project.deploy_error = f"BMC setup failed: {bmc_result}"
                s.commit()
                _delete_deploy_progress(project_id)
                return

        # Step 4c: Create and start containers
        _checkpoint(s, project_id, "containers")
        containers = _extract_containers(topology)
        logger.info(
            "Deploy %s: found %d containers to create", project_id[:8], len(containers)
        )
        if containers:
            _update_deploy_progress(
                project_id, step="containers", detail="Creating containers..."
            )
            logger.info("Deploy %s: creating containers", project_id[:8])

            # Respect start order for containers
            start_order = topology.get("startOrder", [])
            ordered_ids = set()
            for entry in start_order:
                if entry.get("entryType") == "container":
                    ctr_id = entry.get("containerId", entry.get("vmId", ""))
                    ctr = next((c for c in containers if c["node_id"] == ctr_id), None)  # type: ignore[arg-type]
                    if ctr:
                        ordered_ids.add(ctr_id)
                        delay = entry.get("delaySeconds", 0)
                        if delay > 0:
                            _time.sleep(delay)
                        if ctr.get("is_pod"):
                            _create_and_start_pod(
                                host, project_id, ctr, topology, vni_map, pool
                            )
                        else:
                            _create_and_start_container(
                                host, project_id, ctr, topology, vni_map, pool
                            )

            # Create any containers not in start order
            for ctr in containers:
                if ctr["node_id"] not in ordered_ids:
                    if ctr.get("is_pod"):
                        _create_and_start_pod(
                            host, project_id, ctr, topology, vni_map, pool
                        )
                    else:
                        _create_and_start_container(
                            host, project_id, ctr, topology, vni_map, pool
                        )

        if _project_deleted(project_id):
            logger.info(
                "Deploy %s: project deleted mid-deploy, aborting", project_id[:8]
            )
            _delete_deploy_progress(project_id)
            return

        # Step 5: Start VMs (unless auto_start is disabled)
        _checkpoint(s, project_id, "starting")
        if auto_start:
            _update_deploy_progress(project_id, "starting", "VMs")
            notify_project(
                project_id,
                {
                    "type": "deploy-progress",
                    "progress": _get_deploy_progress_data(project_id) or {},
                },
            )
            logger.info("Deploy %s: starting VMs", project_id[:8])
            start_failures = _start_vms_via_troshkad(host, project_id, topology)

            if start_failures:
                failed_names = ", ".join(name for name, _ in start_failures)
                error_msg = f"Failed to start VMs: {failed_names}"
                logger.error("Deploy %s: %s", project_id[:8], error_msg)
                project.state = "error"
                project.deploy_error = error_msg
                from app.services.placement import sync_host_capacity

                sync_host_capacity(s, host)
                s.commit()
                notify_project(
                    project_id,
                    {
                        "type": "project-state",
                        "state": "error",
                        "deploy_error": error_msg,
                    },
                )
                _delete_deploy_progress(project_id)
                return

        project.state = "active" if auto_start else "stopped"
        project.deploy_error = None
        project.deploy_step = None
        project.deploy_progress = None
        project.deployed_topology = project.topology

        # Start auto-stop timer if configured
        if project.state == "active" and project.auto_stop_minutes:
            now = datetime.datetime.now(datetime.UTC)
            project.auto_stop_started_at = now
            project.auto_stop_expires_at = now + datetime.timedelta(
                minutes=project.auto_stop_minutes
            )
            project.auto_stop_warned = False

        # Start auto-delete timer on first deploy
        if project.auto_delete_minutes and not project.auto_delete_started_at:
            now = datetime.datetime.now(datetime.UTC)
            project.auto_delete_started_at = now
            project.lifetime_expires_at = now + datetime.timedelta(
                minutes=project.auto_delete_minutes
            )
            project.auto_delete_warned = False

        # Create DNS records if DNS provider configured
        if project.dns_provider_id and project.guid and project.domain:
            from app.models.dns_provider import DnsProvider
            from app.services.dns_service import create_dns_records, resolve_dns_records

            dns_provider = (
                s.query(DnsProvider).filter_by(id=project.dns_provider_id).first()
            )
            if dns_provider and lb_config:
                _update_deploy_progress(
                    project_id,
                    "dns",
                    f"creating records for {project.guid}.{project.domain}",
                )

                eip_address = None
                for ext_ip in external_ips:
                    pub = ext_ip.get("ip") or ext_ip.get("_public_ip")
                    if pub:
                        eip_address = pub
                        break

                dns_templates = lb_config.get("dns_records", [])
                if dns_templates:
                    records = resolve_dns_records(
                        dns_templates,
                        guid=project.guid,
                        domain=project.domain,
                        eip=eip_address,
                    )
                    errors = create_dns_records(
                        dns_provider.type,
                        dns_provider.config,
                        records,
                        ttl=lb_config.get("dns_ttl", 30),
                    )

                    deployed_topo = project.deployed_topology or {}
                    deployed_topo["_dns_records"] = [
                        r for r in records if r.get("value")
                    ]
                    project.deployed_topology = deployed_topo

                    if errors:
                        logger.warning(
                            "Deploy %s: DNS record creation had errors: %s",
                            project_id[:8],
                            errors,
                        )

        # Store BMC addresses in deployed topology for UI display
        if bmc_config:
            node_map = {n["id"]: n for n in topology.get("nodes", [])}
            deployed_topo = project.deployed_topology or {}
            deployed_topo["bmc"] = {
                "username": bmc_config["bmc_network"].get("bmcUsername", "admin"),
                "password": bmc_config["bmc_network"].get("bmcPassword", "password"),
                "vms": {
                    vm["node_id"]: {
                        "ip": vm["bmc_ip"],
                        "redfish_url": f"redfish-virtualmedia://{vm['bmc_ip']}:8000/redfish/v1/Systems/{node_map.get(vm['node_id'], {}).get('data', {}).get('domainUuid', vm['domain_name'])}",
                        "redfish_url_ssl": f"redfish-virtualmedia+https://{vm['bmc_ip']}:8443/redfish/v1/Systems/{node_map.get(vm['node_id'], {}).get('data', {}).get('domainUuid', vm['domain_name'])}",
                        "ipmi_address": f"{vm['bmc_ip']}:623",
                    }
                    for vm in bmc_config["vms"]
                },
            }
            project.deployed_topology = deployed_topo

        s.commit()
        notify_project(
            project_id,
            {
                "type": "project-state",
                "state": "active",
                "deploy_error": None,
                "auto_stop_expires_at": (
                    project.auto_stop_expires_at.isoformat()
                    if project.auto_stop_expires_at
                    else None
                ),
                "lifetime_expires_at": (
                    project.lifetime_expires_at.isoformat()
                    if project.lifetime_expires_at
                    else None
                ),
            },
        )
        vm_states = {vm["node_id"]: "running" for vm in vms}
        notify_project(
            project_id, {"type": "vm-state", "states": vm_states, "progress": {}}
        )
        _delete_deploy_progress(project_id)
        logger.info("Deploy %s: complete — all VMs running", project_id[:8])

        if auto_start and _has_ocp_monitor(topology):
            project.ocp_status = "monitoring"
            project.ocp_status_detail = None
            project.ocp_install_elapsed = None
            project.ocp_monitor_started_at = datetime.datetime.now(datetime.UTC)
            s.commit()

    except Exception as e:
        logger.exception("Deploy %s failed unexpectedly", project_id[:8])
        _delete_deploy_progress(project_id)
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = str(e)
                if project.host_id:
                    h = s.query(Host).filter_by(id=project.host_id).first()
                    if h:
                        pool = _get_host_pool(h, s)
                        if pool and pool.mode.startswith("shared"):
                            from app.models.storage_pool import SharedCacheEntry

                            for entry in (
                                s.query(SharedCacheEntry)
                                .filter(
                                    SharedCacheEntry.storage_pool_id == pool.id,
                                    SharedCacheEntry.status == "downloading",
                                )
                                .all()
                            ):
                                s.delete(entry)
                s.commit()
                notify_project(
                    project_id,
                    {
                        "type": "project-state",
                        "state": "error",
                        "deploy_error": project.deploy_error,
                    },
                )
        except Exception:
            logger.exception("Deploy %s: failed to set error state", project_id[:8])
    finally:
        if _host_id_for_inflight:
            record_deploy_end(_host_id_for_inflight)
        s.close()


def _clean_kubelet_certs(
    host, project_id, topology, pool, pattern_recert=False, common_password=None
):
    """Regenerate or delete stale kubelet PKI from RHCOS disks before VM startup.

    For SNO (1 RHCOS VM): Uses recert to regenerate all OCP certificates offline,
    reducing boot time from ~15 min to ~2-3 min. Falls back to guestfish on failure
    unless pattern_recert is True (certs are deliberately expired — guestfish won't help).
    For multi-node: Uses guestfish to delete kubelet PKI so it bootstraps fresh.
    Non-fatal — deploy continues regardless of outcome.
    """
    vms = _extract_vms(topology)
    rhcos_vms = [vm for vm in vms if vm.get("os") == "rhcos"]
    if not rhcos_vms:
        return

    recert_vms = [vm for vm in rhcos_vms if vm.get("recertEnabled")]

    bastion_vm = next((v for v in vms if v.get("name") == "bastion"), None)
    bastion_disk_path = None
    if bastion_vm:
        bastion_disks = _find_vm_disks(bastion_vm["node_id"], topology)
        bastion_boot = next(
            (d for d in bastion_disks if d.get("format") == "qcow2"), None
        )
        if bastion_boot:
            bastion_disk_path = _disk_path(
                project_id,
                bastion_vm["node_id"],
                bastion_boot["node_id"],
                bastion_boot["format"],
                pool,
            )

    recert_succeeded = set()
    for i, vm in enumerate(recert_vms):
        vm_disks = _find_vm_disks(vm["node_id"], topology)
        boot_disk = next((d for d in vm_disks if d.get("format") == "qcow2"), None)
        if not boot_disk:
            continue
        disk = _disk_path(
            project_id,
            vm["node_id"],
            boot_disk["node_id"],
            boot_disk["format"],
            pool,
        )
        vm_name = vm.get("name", vm["node_id"][:8])
        logger.info(
            "Deploy %s: running recert on disk for %s (%d/%d)",
            project_id[:8],
            vm_name,
            i + 1,
            len(recert_vms),
        )
        try:
            recert_params = {
                "disk": disk,
                "extend_expiration": True,
                "project_id": project_id,
                "vm_name": vm_name,
            }
            if bastion_disk_path:
                recert_params["bastion_disk"] = bastion_disk_path
            kubeadmin_pw = ""
            if common_password:
                import secrets as _secrets

                import bcrypt

                kubeadmin_pw = common_password
                if len(kubeadmin_pw) < 23:
                    kubeadmin_pw = _secrets.token_urlsafe(24)
                recert_params["common_password"] = kubeadmin_pw
                pw_hash = bcrypt.hashpw(
                    kubeadmin_pw.encode(), bcrypt.gensalt(rounds=12)
                ).decode()
                recert_params["kubeadmin_password_hash"] = pw_hash
            job_id = start_job(host, "/vms/recert", recert_params)
            job = wait_for_job(host, job_id, timeout=300)
            if job.get("status") == "completed":
                logger.info(
                    "Deploy %s: recert completed for %s",
                    project_id[:8],
                    vm_name,
                )
                recert_succeeded.add(vm["node_id"])
                kc = job.get("result", {}).get("kubeconfig")
                for n in topology.get("nodes", []):
                    if n["id"] == vm["node_id"]:
                        if common_password:
                            n.setdefault("data", {})[
                                "ocpKubeadminPassword"
                            ] = kubeadmin_pw
                        if kc:
                            n.setdefault("data", {})["ocpKubeconfig"] = kc
                        break
                continue
            else:
                err = job.get("result", {}).get("error", "unknown")
                if pattern_recert:
                    raise RuntimeError(
                        f"Recert required (pattern has expired certs) but failed for {vm_name}: {err}"
                    )
                logger.warning(
                    "Deploy %s: recert failed for %s: %s — falling back to guestfish",
                    project_id[:8],
                    vm_name,
                    err,
                )
        except RuntimeError:
            raise
        except Exception:
            if pattern_recert:
                raise RuntimeError(
                    f"Recert required (pattern has expired certs) but recert endpoint unavailable for {vm_name}"
                )
            logger.warning(
                "Deploy %s: recert error for %s — falling back to guestfish",
                project_id[:8],
                vm_name,
                exc_info=True,
            )
    if recert_succeeded and len(recert_succeeded) == len(recert_vms):
        return

    operations = [
        {"action": "rm-rf", "path": "/var/lib/kubelet/pki"},
        {"action": "rm-f", "path": "/var/lib/kubelet/kubeconfig"},
    ]

    guestfish_vms = [vm for vm in rhcos_vms if vm["node_id"] not in recert_succeeded]
    for vm in guestfish_vms:
        vm_disks = _find_vm_disks(vm["node_id"], topology)
        boot_disk = next(
            (d for d in vm_disks if d.get("format") == "qcow2"),
            None,
        )
        if not boot_disk:
            logger.warning(
                "Deploy %s: no qcow2 boot disk for RHCOS VM %s, skipping cert cleanup",
                project_id[:8],
                vm.get("name", vm["node_id"][:8]),
            )
            continue

        disk = _disk_path(
            project_id, vm["node_id"], boot_disk["node_id"], boot_disk["format"], pool
        )
        vm_name = vm.get("name", vm["node_id"][:8])
        logger.info(
            "Deploy %s: cleaning kubelet certs from %s", project_id[:8], vm_name
        )
        try:
            job_id = start_job(
                host, "/vms/modify-fs", {"disk": disk, "operations": operations}
            )
            job = wait_for_job(host, job_id, timeout=120)
            if job.get("status") == "failed":
                logger.warning(
                    "Deploy %s: cert cleanup failed for %s: %s",
                    project_id[:8],
                    vm_name,
                    job.get("result", {}).get("error", "unknown"),
                )
            else:
                logger.info(
                    "Deploy %s: cert cleanup complete for %s", project_id[:8], vm_name
                )
        except Exception as e:
            err_msg = str(e)
            if "No such file or directory" in err_msg and "guestfish" in err_msg:
                raise RuntimeError(
                    "guestfish not installed on host — install libguestfs-tools-c"
                ) from e
            logger.warning(
                "Deploy %s: cert cleanup error for %s, continuing",
                project_id[:8],
                vm_name,
                exc_info=True,
            )


def _is_ocp_topology(topology: dict) -> bool:
    nodes = topology.get("nodes", [])
    return any(
        n.get("data", {}).get("os") == "rhcos"
        for n in nodes
        if n.get("type") == "vmNode"
    )


def _has_ocp_monitor(topology: dict) -> bool:
    """Check if any VM in the topology has ocpMonitor or configureBastionBrowser enabled."""
    return any(
        n.get("data", {}).get("ocpMonitor")
        or n.get("data", {}).get("configureBastionBrowser")
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode"
    )


def _is_pattern_deploy(topology: dict) -> bool:
    return any(
        n.get("data", {}).get("patternId")
        for n in topology.get("nodes", [])
        if n.get("type") == "storageNode"
    )


def _parse_node_readiness(result: str | None) -> tuple[list[str], int, int]:
    """Parse ``oc get nodes --no-headers`` output.

    Returns (items, ready_count, total).
    """
    if not result:
        return [], 0, 0
    items: list[str] = []
    ready_count = 0
    total = 0
    for line in result.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2:
            total += 1
            name, status = parts[0], parts[1]
            items.append(f"{name}: {status}")
            if "Ready" in status and "Not" not in status:
                ready_count += 1
    return items, ready_count, total


def _parse_operator_status(result: str | None) -> tuple[list[str], int, int]:
    """Parse ``oc get co --no-headers`` output.

    Returns (items, available_count, total).
    """
    if not result:
        return [], 0, 0
    items: list[str] = []
    available_count = 0
    total = 0
    for line in result.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 4:
            name = parts[0]
            avail = parts[2]
            degraded = parts[4] if len(parts) > 4 else "False"
            total += 1
            if avail == "True":
                available_count += 1
                items.append(f"{name}: available")
            elif degraded == "True":
                items.append(f"{name}: degraded")
            else:
                items.append(f"{name}: progressing")
    return items, available_count, total


def _is_api_error(result: str | None) -> bool:
    """Return True if the oc command result indicates an API error."""
    if not result:
        return True
    lower = result.lower()
    return "error" in lower or "refused" in lower or "connection" in lower


def _resolve_monitor_context(project_id: str):
    """Validate preconditions for OCP health monitor and return context.

    Returns (project, host, topo, deploy_start) if monitor should start, else None.
    """
    from app.core.database import SessionLocal
    from app.models.project import Project

    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(id=project_id).first()
        if (
            not project
            or project.ocp_status != "monitoring"
            or project.state != "active"
        ):
            logger.info(
                "OCP monitor %s: skipped (state=%s, ocp_status=%s)",
                project_id[:8],
                project.state if project else "missing",
                project.ocp_status if project else "missing",
            )
            return None
        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host:
            logger.warning("OCP monitor: host not found for %s", project_id[:8])
            return None
        if host.host_type != "kubevirt-cluster" and host.agent_status != "connected":
            logger.info(
                "OCP monitor %s: skipped (host not connected: type=%s, agent=%s)",
                project_id[:8],
                host.host_type,
                host.agent_status,
            )
            return None
        topo = project.deployed_topology or project.topology or {}
        if not _has_ocp_monitor(topo):
            logger.info(
                "OCP monitor %s: skipped (no ocpMonitor in topo)", project_id[:8]
            )
            return None
        if project.ocp_install_elapsed is not None:
            logger.info("OCP monitor %s: skipped (already completed)", project_id[:8])
            return None
        if project.deploy_started_at:
            deploy_start = project.deploy_started_at.timestamp()
        elif project.ocp_monitor_started_at:
            deploy_start = project.ocp_monitor_started_at.timestamp()
        else:
            deploy_start = 0
        return project, host, topo, deploy_start
    finally:
        db.close()


def _start_vm_monitor(project_id, host_id, node, deploy_start):
    """Start a monitor thread for a single VM if not already running.

    Returns True if the VM was a monitor candidate, False otherwise.
    """
    data = node.get("data", {})
    if not data.get("ocpMonitor") and not data.get("configureBastionBrowser"):
        return False
    vm_id = node["id"]
    monitor_key = f"{project_id}:{vm_id}"
    if is_in_set(_HEALTH_MONITORS_SET, monitor_key):
        logger.info(
            "OCP monitor %s: VM %s already in monitor set, skipping",
            project_id[:8],
            data.get("label", vm_id[:8]),
        )
        return True
    vm_name = data.get("label") or data.get("name", vm_id[:8])
    kc = data.get("ocpKubeconfig")
    add_to_set(_HEALTH_MONITORS_SET, monitor_key, ttl=86400)
    threading.Thread(
        target=_monitor_ocp_vm_health,
        args=(project_id, host_id, vm_id, vm_name, kc, deploy_start),
        daemon=True,
        name=f"ocp-vm-{project_id[:8]}-{vm_name}",
    ).start()
    logger.info("OCP VM monitor started for %s/%s", project_id[:8], vm_name)
    return True


def maybe_start_ocp_health_monitor(project_id: str):
    """Start OCP health monitor if project needs it and one isn't already running."""
    logger.info("maybe_start_ocp_health_monitor called for %s", project_id[:8])
    ctx = _resolve_monitor_context(project_id)
    if not ctx:
        return
    _project, host, topo, deploy_start = ctx

    vm_candidates = 0
    for node in topo.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        if _start_vm_monitor(project_id, host.id, node, deploy_start):
            vm_candidates += 1
    if vm_candidates == 0:
        logger.info(
            "OCP monitor %s: no VMs with ocpMonitor found in topo", project_id[:8]
        )


def _exec_oc(host, project_id: str, command: str, timeout: int = 15):
    """Run an oc command directly — no bastion needed."""
    if host.host_type == "kubevirt-cluster":
        from app.core.database import SessionLocal
        from app.models.provider import Provider

        db = SessionLocal()
        try:
            provider = db.query(Provider).filter_by(id=host.provider_id).first()
            if not provider:
                raise RuntimeError("Provider not found")
            from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

            _, core_v1, _ = _get_k8s_clients(provider)
            namespace = _project_ns(provider, project_id)
            from app.services.providers.kubevirt import _find_exec_pod

            exec_pod = _find_exec_pod(core_v1, namespace, project_id)
            if not exec_pod:
                raise RuntimeError("No exec pod found")
            from kubernetes.stream import stream as k8s_stream

            result = k8s_stream(
                core_v1.connect_get_namespaced_pod_exec,
                exec_pod.metadata.name,
                namespace,
                container="exec",
                command=[
                    "sh",
                    "-c",
                    f"export KUBECONFIG=/root/.kube/config; {command}",
                ],
                stderr=True,
                stdout=True,
                stdin=False,
                tty=False,
                _preload_content=True,
                _request_timeout=timeout + 5,
            )
            return result.strip() if isinstance(result, str) else ""
        finally:
            db.close()
    else:
        from app.services.troshkad_client import start_job, wait_for_job

        job_id = start_job(
            host,
            "/oc-exec",
            {
                "project_id": project_id,
                "command": command,
                "timeout": timeout,
            },
        )
        job = wait_for_job(host, job_id, timeout=timeout + 15)
        if job.get("status") == "completed":
            result = job.get("result", {})
            if result.get("exit_code", 1) != 0:
                raise RuntimeError(
                    result.get("error") or result.get("output") or "oc-exec failed"
                )
            return result.get("output", "")
        raise RuntimeError(job.get("result", {}).get("error", "oc-exec failed"))


_ocpvirt_hosts: dict[str, bool] = {}


def _is_ocpvirt_host(host) -> bool:
    """Check if a host is on an ocpvirt provider (cached)."""
    if host.id in _ocpvirt_hosts:
        return _ocpvirt_hosts[host.id]
    from app.core.database import SessionLocal
    from app.models.provider import Provider

    db = SessionLocal()
    try:
        prov = db.query(Provider).filter_by(id=host.provider_id).first()
        result = prov.type == "ocpvirt" if prov else False
    finally:
        db.close()
    _ocpvirt_hosts[host.id] = result
    return result


def _exec_on_bastion(
    host,
    project_id: str,
    bastion_ip: str,
    password: str,
    command: str,
    timeout: int = 15,
):
    # Try direct oc for simple oc/kubectl commands (no bastion needed).
    # - kubevirt: exec pod has network access + mounted kubeconfig
    # - troshkad: oc-exec uses unshare --mount to set DNS to project dnsmasq
    # Skip shell pipelines (|, &&, ;) — those need the bastion SSH path.
    cmd_stripped = command.strip()
    if cmd_stripped.startswith(("oc ", "kubectl ")) and not any(
        c in command for c in ("|", "&&", ";")
    ):
        try:
            return _exec_oc(host, project_id, command, timeout)
        except Exception:
            pass

    if host.host_type == "kubevirt-cluster":
        return _exec_on_bastion_kubevirt(
            host, project_id, bastion_ip, password, command, timeout
        )
    return _exec_on_bastion_troshkad(
        host, project_id, bastion_ip, password, command, timeout
    )


def _exec_on_bastion_kubevirt(host, project_id, bastion_ip, password, command, timeout):
    import re as _re

    try:
        from app.core.database import SessionLocal
        from app.models.provider import Provider

        db = SessionLocal()
        try:
            provider = db.query(Provider).filter_by(id=host.provider_id).first()
        finally:
            db.close()
        if not provider:
            return None

        from kubernetes.stream import stream as k8s_stream

        from app.services.providers.kubevirt import (
            _find_exec_pod,
            _get_k8s_clients,
            _project_ns,
        )

        _, core_v1, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)
        exec_pod = _find_exec_pod(core_v1, namespace, project_id)
        if not exec_pod:
            return None

        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            f"ConnectTimeout={min(timeout, 10)}",
            "-i",
            "/root/.ssh/id_ed25519",
            f"cloud-user@{bastion_ip}",
            command,
        ]
        resp = k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            exec_pod.metadata.name,
            namespace,
            command=ssh_cmd,
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=True,
            _request_timeout=timeout + 10,
        )
        output = resp or ""
        if output:
            output = _re.sub(r"\x1b\[[0-9;]*m", "", output)
            lines = [
                l
                for l in output.split("\n")
                if l.strip()
                and not l.strip().startswith("OpenShift Console:")
                and not l.strip().startswith("Username:")
                and not l.strip().startswith("Password:")
            ]
            output = "\n".join(lines)
        return output or None
    except Exception:
        return None


def _exec_on_bastion_troshkad(host, project_id, bastion_ip, password, command, timeout):
    import re as _re

    try:
        job_id = start_job(
            host,
            "/vm/ssh-exec",
            {
                "project_id": project_id,
                "vm_ip": bastion_ip,
                "username": "cloud-user",
                "password": password,
                "command": command,
                "timeout": timeout,
            },
        )
        job = wait_for_job(host, job_id, timeout=timeout + 15)
        if job["status"] == "completed":
            result = job.get("result", {})
            if result.get("output"):
                output = _re.sub(r"\x1b\[[0-9;]*m", "", result["output"])
                lines = [
                    l
                    for l in output.split("\n")
                    if l.strip()
                    and not l.strip().startswith("OpenShift Console:")
                    and not l.strip().startswith("Username:")
                    and not l.strip().startswith("Password:")
                ]
                result["output"] = "\n".join(lines)
            if not result.get("error"):
                return result.get("output", "")
    except TroshkadError:
        pass
    return None


def _verify_bastion_browser(exec_fn, push_fn, project_id, vm_name=None):
    """Verify and fix bastion CA trust and browser credentials.

    Args:
        exec_fn: callable(command, timeout) that runs shell commands on the bastion.
        push_fn: callable(phase, detail) for progress updates.
        project_id: project ID for logging.
        vm_name: optional VM name for logging context.

    Returns True if bastion is ready, False otherwise.
    """
    import time as _t

    label = f"{project_id[:8]}/{vm_name}" if vm_name else project_id[:8]
    _VERIFY_SCRIPT = (
        "LIVE_FP=$(oc get secret -n openshift-ingress router-certs-default "
        "  -o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d "
        "  | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2); "
        "FILE_FP=$(openssl x509 -noout -fingerprint -sha256 "
        "  -in /etc/pki/ca-trust/source/anchors/ocp-ingress.pem 2>/dev/null | cut -d= -f2); "
        'if [ -n "$LIVE_FP" ] && [ "$LIVE_FP" = "$FILE_FP" ]; then echo "ca:ok"; '
        'elif [ -z "$LIVE_FP" ]; then echo "ca:pending"; '
        'else echo "ca:stale"; fi; '
        'BOOT=$(date -d "$(uptime -s)" +%s 2>/dev/null || echo 0); '
        "LJ=$(stat -c %Y /home/cloud-user/.mozilla/firefox/*/logins.json 2>/dev/null | head -1 || echo 0); "
        'if [ "$LJ" -gt "$BOOT" ] 2>/dev/null; then echo "logins:ok"; '
        'elif [ "$LJ" = "0" ]; then echo "logins:missing"; '
        'else echo "logins:stale"; fi'
    )
    _CA_UPDATE_CMD = (
        "oc get secret -n openshift-ingress router-certs-default "
        "-o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d "
        "| sudo tee /etc/pki/ca-trust/source/anchors/ocp-ingress.pem >/dev/null "
        "&& sudo update-ca-trust"
    )
    _AUTOLOGIN_CMD = (
        "CONSOLE_URL=$(oc whoami --show-console 2>/dev/null); "
        '[ -n "$CONSOLE_URL" ] && [ -f /home/cloud-user/ocp-autologin.py ] && '
        'python3 /home/cloud-user/ocp-autologin.py "$CONSOLE_URL" 2>&1 || true'
    )

    for _ in range(18):
        verify = exec_fn(_VERIFY_SCRIPT, timeout=20)
        if verify and "ca:ok" in verify and "logins:ok" in verify:
            return True

        if _apply_bastion_browser_fixes(
            verify, exec_fn, push_fn, _CA_UPDATE_CMD, _AUTOLOGIN_CMD
        ):
            _t.sleep(5)
            continue
        push_fn("browser", "waiting for bastion setup")
        _t.sleep(10)

    logger.warning("OCP monitor %s: bastion browser setup incomplete", label)
    return False


def _apply_bastion_browser_fixes(verify, exec_fn, push_fn, ca_cmd, autologin_cmd):
    """Check for and apply CA cert / browser credential fixes. Returns True if fixes applied."""
    needs_fix = []
    if verify and "ca:stale" in verify:
        needs_fix.append(_MSG_CA_CERT)
    if verify and ("logins:stale" in verify or "logins:missing" in verify):
        needs_fix.append(_MSG_BROWSER_CREDS)
    if not needs_fix:
        return False
    push_fn("browser", f"bastion {', '.join(needs_fix)} stale, updating...")
    if _MSG_CA_CERT in needs_fix:
        exec_fn(ca_cmd, timeout=15)
    if _MSG_BROWSER_CREDS in needs_fix:
        exec_fn(autologin_cmd, timeout=30)
    return True


def _approve_pending_csrs(host, project_id, bastion_ip, password):
    """Approve any pending OCP CSRs on the cluster. Returns count approved."""
    result = _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        password,
        "oc get csr --no-headers 2>/dev/null",
        timeout=10,
    )
    if not result:
        return 0
    pending_names = []
    for line in result.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 4 and "Pending" in line:
            pending_names.append(parts[0])
    if not pending_names:
        return 0
    for name in pending_names:
        _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            f"oc adm certificate approve {name} 2>/dev/null",
            timeout=10,
        )
    logger.info(
        "Approved %d pending CSR(s) for project %s",
        len(pending_names),
        project_id[:8],
    )
    return len(pending_names)


def _monitor_ocp_health(
    project_id: str, host_id: str, topology: dict, deploy_start: float = 0
):
    from app.core.database import SessionLocal as _SL2

    _mon_db = _SL2()
    try:
        _ocp_health_inner(project_id, host_id, topology, deploy_start, _mon_db)
    except Exception as e:
        logger.exception("OCP health monitor %s failed: %s", project_id[:8], e)
    finally:
        remove_from_set(_HEALTH_MONITORS_SET, project_id)
        _mon_db.close()


def _monitor_ocp_vm_health(
    project_id: str,
    host_id: str,
    vm_id: str,
    vm_name: str,
    kubeconfig_content: str,
    deploy_start: float = 0,
):
    """Monitor a single OCP cluster identified by its kubeconfig (e.g. a SNO).

    Writes the kubeconfig to a temp file on the bastion, then monitors
    node readiness, CSR approval, and operator availability.
    """
    from app.core.database import SessionLocal as _SL3

    monitor_key = f"{project_id}:{vm_id}"
    db = _SL3()
    try:
        _ocp_vm_health_inner(
            project_id, host_id, vm_id, vm_name, kubeconfig_content, deploy_start, db
        )
    except Exception as e:
        logger.exception("OCP VM monitor %s/%s failed: %s", project_id[:8], vm_name, e)
    finally:
        remove_from_set(_HEALTH_MONITORS_SET, monitor_key)
        db.close()


def _extract_bastion_info(nodes):
    """Extract bastion node, IP, and password from topology nodes."""
    bastion = next(
        (
            n
            for n in nodes
            if n.get("type") == "vmNode" and n.get("data", {}).get("label") == "bastion"
        ),
        None,
    )
    bastion_ip = ""
    password = ""
    if bastion:
        for nic in bastion.get("data", {}).get("nics", []):
            if nic.get("ip"):
                bastion_ip = nic["ip"]
                break
        password = bastion.get("data", {}).get("ciCloudUserPassword", "")
    return bastion, bastion_ip, password


def _approve_csrs_if_due(approve_fn, push_fn, last_check, interval=30):
    """Approve pending CSRs if enough time has elapsed. Returns updated timestamp."""
    import time as _t

    now = _t.time()
    if now - last_check < interval:
        return last_check
    approved = approve_fn()
    if approved:
        push_fn("certs", f"approved {approved} certificate(s)")
    return now


def _ocp_vm_wait_for_operators(oc_fn, approve_fn, push_fn, deadline):
    """Wait for cluster operators to become available, approving CSRs along the way."""
    import time as _t

    push_fn("operators", "waiting for cluster operators")
    last_csr_check_ops = 0.0
    while _t.time() < deadline:
        last_csr_check_ops = _approve_csrs_if_due(
            approve_fn, push_fn, last_csr_check_ops
        )
        result = oc_fn("oc get co --no-headers 2>/dev/null", timeout=15)
        if not _is_api_error(result):
            items, available_count, total = _parse_operator_status(result)
            if total > 0:
                push_fn(
                    "operators",
                    f"{available_count}/{total} operators available",
                    items,
                )
                if available_count >= total:
                    break
        else:
            push_fn("operators", _MSG_WAITING_API)
        _t.sleep(10)


def _ocp_vm_poll_with_csrs(oc_fn, approve_fn, push_fn, deadline):
    """Poll for nodes Ready + operators available, approving CSRs along the way."""
    import time as _t

    # Wait for nodes Ready
    push_fn("nodes", "waiting for nodes to be Ready")
    last_csr_check = 0.0
    while _t.time() < deadline:
        last_csr_check = _approve_csrs_if_due(approve_fn, push_fn, last_csr_check)
        result = oc_fn(_CMD_GET_NODES, timeout=10)
        if not _is_api_error(result):
            items, ready_count, total = _parse_node_readiness(result)
            if total > 0:
                push_fn("nodes", f"{ready_count}/{total} ready", items)
                if ready_count >= total:
                    break
        else:
            push_fn("nodes", _MSG_WAITING_API)
        _t.sleep(5)

    # Wait for cluster operators
    _ocp_vm_wait_for_operators(oc_fn, approve_fn, push_fn, deadline)


def _check_vm_route_http(oc_fn, route_prefix):
    """Curl a route via oc_fn and return the HTTP status code string."""
    raw = (
        oc_fn(
            f"curl -skm 10 -o /dev/null -w '%{{http_code}}' "
            f"https://{route_prefix}."
            "$(oc whoami --show-server 2>/dev/null | sed 's|https://api\\.||;s|:6443||') "
            "2>/dev/null || echo 000",
            timeout=20,
        )
        or "000"
    ).strip()
    return raw


def _is_route_ready(code):
    """Return True if the HTTP code indicates the route is responding."""
    return code.startswith(("2", "3")) or code == "403"


def _http_suffix(code):
    """Return a formatted HTTP code suffix for status messages."""
    return f" (HTTP {code})" if code not in ("000", "") else ""


def _check_vm_console_and_oauth(oc_fn, push_fn, _t):
    """Check console and OAuth routes. Returns True if both ready, False otherwise."""
    push_fn("console", "console operator available, verifying route...")
    console_code = _check_vm_route_http(oc_fn, "console-openshift-console.apps")
    if not _is_route_ready(console_code):
        push_fn("console", f"waiting for console route{_http_suffix(console_code)}")
        _t.sleep(10)
        return False
    oauth_code = _check_vm_route_http(oc_fn, "oauth-openshift.apps")
    if not _is_route_ready(oauth_code):
        push_fn("console", f"waiting for OAuth route{_http_suffix(oauth_code)}")
        _t.sleep(10)
        return False
    push_fn("console", "console and OAuth ready")
    return True


def _ocp_vm_wait_for_console(oc_fn, approve_fn, push_fn, deadline):
    """Wait for console and OAuth routes to respond."""
    import time as _t

    push_fn("console", _MSG_WAITING_CONSOLE)
    last_csr_check_console = 0.0
    while _t.time() < deadline:
        last_csr_check_console = _approve_csrs_if_due(
            approve_fn, push_fn, last_csr_check_console
        )
        result = oc_fn("oc get co console --no-headers 2>/dev/null", timeout=15)
        if result and "error" not in result.lower():
            parts = result.strip().split()
            co_available = parts[2] == "True" if len(parts) >= 4 else False
            if co_available:
                if _check_vm_console_and_oauth(oc_fn, push_fn, _t):
                    return
                continue
        push_fn("console", _MSG_WAITING_CONSOLE)
        _t.sleep(5)


def _configure_bastion_and_cleanup(
    nodes,
    vm_id,
    kc_path,
    host,
    project_id,
    bastion_ip,
    password,
    _oc,
    _push,
    vm_name=None,
):
    """Configure bastion browser if flag is set, then clean up temp kubeconfig."""
    vm_node = next((n for n in nodes if n["id"] == vm_id), None)
    configure_browser = vm_node and vm_node.get("data", {}).get(
        "configureBastionBrowser"
    )
    if configure_browser:
        # Copy kubeconfig to bastion default locations (skip if already using bastion default)
        if kc_path:
            _push("browser", "setting bastion kubeconfig for this cluster")
            _exec_on_bastion(
                host,
                project_id,
                bastion_ip,
                password,
                f"mkdir -p /home/cloud-user/ocp-install/auth /home/cloud-user/.kube;"
                f" cp {kc_path} /home/cloud-user/ocp-install/auth/kubeconfig;"
                f" cp {kc_path} /home/cloud-user/.kube/config;"
                f" chown -R cloud-user:cloud-user"
                " /home/cloud-user/ocp-install /home/cloud-user/.kube 2>/dev/null || true",
                timeout=10,
            )

        # Refresh bastion CA trust with this cluster's ingress cert
        _push("browser", "refreshing bastion CA trust")
        _oc(
            "oc get secret -n openshift-ingress router-certs-default "
            "-o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d "
            "| sudo tee /etc/pki/ca-trust/source/anchors/ocp-ingress.pem >/dev/null "
            "&& sudo update-ca-trust",
            timeout=15,
        )

        # Verify CA fingerprint + run autologin with retry loop
        _push("browser", "verifying bastion browser setup")
        bastion_ready = _verify_bastion_browser(_oc, _push, project_id, vm_name)
        if bastion_ready:
            _push("browser", "bastion browser ready")

    # Cleanup temp kubeconfig (skip if we used bastion default or copied it there)
    if kc_path and not configure_browser:
        _exec_on_bastion(
            host, project_id, bastion_ip, password, f"rm -f {kc_path}", timeout=5
        )


def _setup_bastion_kubeconfig(
    host, project_id, vm_id, bastion_ip, password, kubeconfig_content
):
    """Write kubeconfig to bastion if provided, return effective kc path."""
    kc_path = None
    if kubeconfig_content:
        kc_path = f"/tmp/troshka-kc-{vm_id[:8]}.yaml"
        import base64

        kc_b64 = base64.b64encode(kubeconfig_content.encode()).decode()
        _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            f"echo '{kc_b64}' | base64 -d > {kc_path}",
            timeout=10,
        )

    bastion_kc = "/home/cloud-user/ocp-install/auth/kubeconfig"
    return kc_path, kc_path or bastion_kc


def _make_oc_and_csr_helpers(
    host, project_id, bastion_ip, password, effective_kc, vm_name
):
    """Build _oc and _approve_csrs closures for bastion exec."""

    def _oc(cmd, timeout=15):
        return _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            f"export KUBECONFIG={effective_kc}; {cmd}",
            timeout=timeout,
        )

    def _approve_csrs():
        result = _oc("oc get csr --no-headers 2>/dev/null", timeout=10)
        if not result:
            return 0
        pending = [
            l.split()[0]
            for l in result.strip().split("\n")
            if l.split() and len(l.split()) >= 4 and "Pending" in l
        ]
        for name in pending:
            _oc(f"oc adm certificate approve {name} 2>/dev/null", timeout=10)
        if pending:
            logger.info(
                "VM monitor %s/%s: approved %d CSR(s)",
                project_id[:8],
                vm_name,
                len(pending),
            )
        return len(pending)

    return _oc, _approve_csrs


def _ocp_vm_wait_for_api(_oc, _push, deadline):
    import time as _t

    _push("oc", _MSG_WAITING_API)
    while _t.time() < deadline:
        try:
            result = _oc(_CMD_GET_NODES, timeout=10)
            if result and "Ready" in result:
                return True
        except Exception:
            pass
        _push("oc", _MSG_WAITING_API)
        _t.sleep(10)
    _push("timeout", "API server not reachable")
    return False


def _ocp_vm_restart_ingress(_oc, _push):
    _push("console", "restarting ingress router")
    _oc(
        "oc rollout restart deployment/router-default -n openshift-ingress 2>/dev/null || true",
        timeout=15,
    )


def _ocp_vm_final_csr_sweep(_approve_csrs, _push):
    import time as _t

    for _ in range(6):
        approved = _approve_csrs()
        if not approved:
            break
        _push("certs", f"approved {approved} certificate(s)")
        _t.sleep(10)


def _ocp_vm_health_inner(
    project_id, host_id, vm_id, vm_name, kubeconfig_content, deploy_start, db
):
    import time as _t

    from app.models.host import Host as _Host3
    from app.models.project import Project as _VmProj

    logger.info(
        "OCP VM monitor %s/%s: inner started (host=%s)",
        project_id[:8],
        vm_name,
        host_id[:8],
    )
    host = db.query(_Host3).filter_by(id=host_id).first()
    if not host:
        logger.warning(
            "OCP VM monitor %s/%s: host %s not found",
            project_id[:8],
            vm_name,
            host_id[:8],
        )
        return

    project = db.query(_VmProj).filter_by(id=project_id).first()
    if not project or project.state != "active":
        logger.warning(
            "OCP VM monitor %s/%s: project not found or not active (state=%s)",
            project_id[:8],
            vm_name,
            project.state if project else "missing",
        )
        return
    topo = project.deployed_topology or project.topology or {}

    start = deploy_start or _t.time()

    def _elapsed():
        s = int(_t.time() - start)
        return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"

    def _push(phase, detail, items=None):
        detail_with_time = f"{detail} ({_elapsed()})"
        msg = {
            "type": "ocp-health",
            "phase": phase,
            "detail": detail_with_time,
            "vm_id": vm_id,
            "vm_name": vm_name,
        }
        if items:
            msg["items"] = items
        notify_project(project_id, msg)

    # Find bastion for exec
    nodes = topo.get("nodes", [])
    _bastion, bastion_ip, password = _extract_bastion_info(nodes)

    if not bastion_ip:
        logger.warning(
            "OCP VM monitor %s/%s: no bastion — cannot monitor",
            project_id[:8],
            vm_name,
        )
        return

    kc_path, effective_kc = _setup_bastion_kubeconfig(
        host, project_id, vm_id, bastion_ip, password, kubeconfig_content
    )
    _oc, _approve_csrs = _make_oc_and_csr_helpers(
        host, project_id, bastion_ip, password, effective_kc, vm_name
    )

    deadline = _t.time() + 1800
    logger.info("OCP VM monitor started for %s/%s", project_id[:8], vm_name)

    if not _ocp_vm_wait_for_api(_oc, _push, deadline):
        return

    _ocp_vm_poll_with_csrs(_oc, _approve_csrs, _push, deadline)
    _ocp_vm_restart_ingress(_oc, _push)
    _t.sleep(10)
    _ocp_vm_wait_for_console(_oc, _approve_csrs, _push, deadline)
    _ocp_vm_final_csr_sweep(_approve_csrs, _push)

    # Configure bastion browser + cleanup temp kubeconfig
    _configure_bastion_and_cleanup(
        nodes,
        vm_id,
        kc_path,
        host,
        project_id,
        bastion_ip,
        password,
        _oc,
        _push,
        vm_name=vm_name,
    )

    _push("ready", f"{vm_name} cluster ready")
    logger.info(
        "OCP VM monitor complete for %s/%s (%s)",
        project_id[:8],
        vm_name,
        _elapsed(),
    )


def _ocp_update_status(project_id, status, elapsed_secs=None):
    """Update ocp_status (and optionally ocp_install_elapsed) in the DB."""
    try:
        from app.core.database import SessionLocal
        from app.models.project import Project

        db = SessionLocal()
        p = db.query(Project).filter_by(id=project_id).first()
        if p:
            p.ocp_status = status
            if elapsed_secs is not None:
                p.ocp_install_elapsed = elapsed_secs
            db.commit()
        db.close()
    except Exception:
        logger.exception("Failed to update ocp_status for %s", project_id[:8])


def _extract_dns_domain(nodes):
    """Extract DNS domain from network node records, defaulting to ocp.ocp.local."""
    for n in nodes:
        if n.get("type") != "networkNode":
            continue
        for rec in n.get("data", {}).get("dnsRecords", []):
            name = rec.get("name", "")
            if name.startswith("api."):
                return name[4:]
    return "ocp.ocp.local"


def _ocp_extract_topology_info(topology):
    """Extract bastion info, control-plane nodes, and DNS domain from topology."""
    nodes = topology.get("nodes", [])

    bastion = next(
        (
            n
            for n in nodes
            if n.get("type") == "vmNode" and n.get("data", {}).get("label") == "bastion"
        ),
        None,
    )
    bastion_ip = ""
    password = ""
    if bastion:
        for nic in bastion.get("data", {}).get("nics", []):
            if nic.get("ip"):
                bastion_ip = nic["ip"]
                break
        password = bastion.get("data", {}).get("ciCloudUserPassword", "")

    cp_nodes = [
        n
        for n in nodes
        if n.get("type") == "vmNode" and n.get("data", {}).get("os") == "rhcos"
    ]
    cp_names = [n.get("data", {}).get("label", n["id"][:8]) for n in cp_nodes]

    dns_domain = _extract_dns_domain(nodes)

    return bastion, bastion_ip, password, cp_names, dns_domain


def _ocp_wait_for_bastion_ssh(
    host, project_id, bastion, bastion_ip, password, push_fn, deadline
):
    """Phase 1: wait for bastion SSH to become available. Returns True if ready."""
    import time as _t

    if host.host_type != "kubevirt-cluster":
        from app.services.troshkad_client import get_vm_state as _get_vm_st

        bastion_dom = _vm_domain_name(project_id, bastion["id"])
        try:
            vm_info = _get_vm_st(host, bastion_dom, timeout=5)
            if vm_info.get("state") in ("shut_off", "shutoff"):
                push_fn(
                    "waiting",
                    "bastion is powered off — start it to enable OCP monitoring",
                )
                return False
        except Exception:
            pass

    push_fn("ssh", "waiting for bastion")
    while _t.time() < deadline:
        result = _exec_on_bastion(
            host, project_id, bastion_ip, password, "echo ok", timeout=5
        )
        if result and "ok" in result:
            return True
        push_fn("ssh", "waiting for bastion")
        _t.sleep(5)

    push_fn("timeout", "bastion SSH not available")
    return False


def _ocp_wait_for_direct_oc(host, project_id, push_fn, deadline):
    """Phase 1 (no bastion): wait for direct oc access. Returns True if ready."""
    import time as _t

    push_fn("oc", "waiting for OCP API")
    while _t.time() < deadline:
        try:
            result = _exec_oc(host, project_id, "get nodes --no-headers", timeout=10)
            if result and "Ready" in result:
                return True
        except Exception:
            pass
        push_fn("oc", "waiting for OCP API")
        _t.sleep(10)

    push_fn("timeout", "OCP API not reachable")
    return False


def _detect_install_phases(full_text, phases_seen):
    """Detect install phases from log text markers and update phases_seen in place."""
    _phase_markers = [
        ("Downloading openshift-install", "downloading"),
        ("Downloaded openshift-install", "downloaded"),
        ("Creating agent ISO", "creating-iso"),
        ("Agent Rest API Initialized", "api-init"),
    ]
    for marker, phase in _phase_markers:
        if marker in full_text:
            phases_seen.add(phase)

    _compound_markers = [
        (["Extracting base ISO", "Base ISO obtained"], "extracting-iso"),
        (["Generated ISO at", "Agent ISO created"], "iso-ready"),
        (["Waiting for cluster install to initialize"], "waiting-init"),
        (["validation:"], "validating"),
        (["Bootstrap Kube API Initialized"], "bootstrap-api"),
        (["Bootstrap is complete", "cluster bootstrap is complete"], "bootstrap"),
        (["Working towards"], "control-plane"),
        (["Cluster is initialized"], "initialized"),
    ]
    for markers, phase in _compound_markers:
        if any(m in full_text for m in markers):
            phases_seen.add(phase)

    if "Booted" in full_text and "from ISO" in full_text:
        phases_seen.add("nodes-booted")
    if "preparing-for-installation" in full_text or "Preparing cluster" in full_text:
        phases_seen.add("validation")
        phases_seen.add("preparing")
    if "Waiting up to" in full_text and "to initialize" in full_text:
        phases_seen.add("bootstrap")


def _ocp_parse_install_phases(
    full_text, phases_seen, cp_names, tracked_ops, op_aliases
):
    """Parse install.log text and return (items, detail, phases_seen, node_status)."""
    import re as _re

    _detect_install_phases(full_text, phases_seen)

    # Parse per-node status from log
    node_status = _ocp_parse_node_status(full_text, cp_names)

    # Build progress items list
    items = _ocp_build_progress_items(
        phases_seen, cp_names, node_status, full_text, tracked_ops, op_aliases, _re
    )

    # Build summary detail line
    detail = _ocp_build_summary_detail(phases_seen, full_text)

    return items, detail, phases_seen, node_status


def _classify_node_msg(msg):
    """Classify a node log message into a status string, or None if unrecognized."""
    if "Writing image to disk: 100%" in msg:
        return "written"
    if "Writing image to disk" in msg:
        pct = (
            msg.split("Writing image to disk:")[-1].strip().rstrip("%")
            if ":" in msg
            else ""
        )
        return f"writing {pct}%"
    if "Rebooting" in msg:
        return "rebooting"
    if "Waiting for bootkube" in msg:
        return "bootkube"
    if "Configuring" in msg:
        return "configuring"
    if "Joined" in msg:
        return "joined"
    if "Done" in msg or "completing installation" in msg:
        return "done"
    return None


def _line_mentions_host(line, cp):
    """Check if a log line references a specific control-plane host."""
    return f"Host {cp}" in line or f"Host: {cp}" in line or f"Node {cp}" in line


def _update_node_status_from_line(line, cp_names, node_status):
    """Check a single log line against all CP names and update node_status."""
    for cp in cp_names:
        if not _line_mentions_host(line, cp):
            continue
        msg = line.split("msg=")[-1] if "msg=" in line else line
        status = _classify_node_msg(msg)
        if status is not None:
            if status.startswith("writing"):
                node_status.setdefault(cp, status)
            else:
                node_status[cp] = status


def _ocp_parse_node_status(full_text, cp_names):
    """Parse per-node status from install.log lines."""
    node_status = {}
    for line in full_text.split("\n"):
        _update_node_status_from_line(line, cp_names, node_status)
    return node_status


def _cluster_init_status(phases_seen):
    """Determine cluster init icon based on phases seen."""
    if "api-init" in phases_seen or "validation" in phases_seen:
        return "✓"
    if "waiting-init" in phases_seen:
        return "⏳"
    return "—"


def _build_node_install_items(node_status, cp_names):
    """Build progress items for the node installation sub-phase."""
    items = []
    all_done = all(s in ("done", "joined") for s in node_status.values())
    items.append(f"Installing nodes: {'✓' if all_done else '⏳'}")
    for cp in cp_names:
        s = node_status.get(cp, "—")
        items.append(f"  {cp}: {s}")
    return items


def _phase_icon(phases_seen, done_phase):
    """Return ✓ if done_phase is in phases_seen, else ⏳."""
    return "✓" if done_phase in phases_seen else "⏳"


def _build_early_phase_items(phases_seen, node_status, cp_names):
    """Build progress items for early phases: download, ISO, boot, validation, nodes."""
    items = []
    if "downloading" in phases_seen:
        items.append(f"Download OCP tools: {_phase_icon(phases_seen, 'downloaded')}")
    if "creating-iso" in phases_seen or "downloaded" in phases_seen:
        items.append(f"Build agent ISO: {_phase_icon(phases_seen, 'iso-ready')}")
    if "iso-ready" in phases_seen:
        items.append(f"Boot nodes from ISO: {_phase_icon(phases_seen, 'nodes-booted')}")
    if "nodes-booted" in phases_seen:
        items.append(f"Cluster init: {_cluster_init_status(phases_seen)}")

    if "validation" in phases_seen:
        items.append("Host validation: ✓")
    elif "validating" in phases_seen or "api-init" in phases_seen:
        items.append("Host validation: ⏳")

    if "preparing" in phases_seen:
        has_installing = bool(node_status)
        items.append(f"Preparing for installation: {'✓' if has_installing else '⏳'}")

    if node_status:
        items.extend(_build_node_install_items(node_status, cp_names))

    return items


def _control_plane_detail(phases_seen, full_text, _re):
    """Determine the control-plane version/status detail string."""
    cp_detail = "⏳"
    for line in reversed(full_text.split("\n")):
        if "Working towards" in line:
            msg = line.split("msg=")[-1] if "msg=" in line else line
            m = _re.search(r"([\d.]+)", msg)
            if m:
                cp_detail = f"OCP {m.group(1)} ⏳"
            break
    if "initialized" in phases_seen:
        cp_detail = cp_detail.replace(" ⏳", " ✓")
    return cp_detail


def _build_bootstrap_items(phases_seen, node_status, full_text, _re):
    """Build progress items for bootstrap, API, and control-plane phases."""
    items = []
    has_bootkube = any(s == "bootkube" for s in node_status.values())
    has_configuring = any(
        s in ("configuring", "joined", "done") for s in node_status.values()
    )

    if has_bootkube or has_configuring or "bootstrap-api" in phases_seen:
        items.append(
            f"etcd: {'✓' if has_configuring or 'bootstrap' in phases_seen else '⏳'}"
        )

    if "bootstrap" in phases_seen:
        items.append("Bootstrap: ✓")
    elif "bootstrap-api" in phases_seen or has_bootkube:
        items.append("Bootstrap: ⏳")
    elif node_status:
        items.append("Bootstrap: —")

    if "bootstrap" in phases_seen and "control-plane" not in phases_seen:
        items.append("API: ⏳")

    if "control-plane" in phases_seen:
        items.append("API: ✓")
        cp_detail = _control_plane_detail(phases_seen, full_text, _re)
        items.append(f"Cluster init: {cp_detail}")
    elif "bootstrap" in phases_seen:
        items.append("Cluster init: —")

    return items


def _match_unavailable_ops(msg, tracked_ops, op_aliases):
    """Match operator names and aliases in an unavailability message."""
    not_available = set()
    for real_name, alias in op_aliases.items():
        if real_name in msg:
            not_available.add(alias)
    for op in tracked_ops:
        if op in msg:
            not_available.add(op)
    return not_available


def _parse_unavailable_operators(full_text, tracked_ops, op_aliases):
    """Parse the latest 'not available' line to find unavailable operators."""
    for line in reversed(full_text.split("\n")):
        if (
            "are not available" in line or "is not available" in line
        ) and "Cluster operator" in line:
            msg = line.split("msg=")[-1] if "msg=" in line else line
            return _match_unavailable_ops(msg, tracked_ops, op_aliases)
    return set()


def _build_operator_items(phases_seen, tracked_ops, not_available):
    """Build progress items for operator status."""
    items = []
    if "initialized" in phases_seen:
        items.append("Cluster operators: ✓")
    elif not_available:
        phases_seen.add("operators")
        avail = len(tracked_ops) - len(not_available)
        items.append(f"Cluster operators: {avail}/{len(tracked_ops)}")
        for op in tracked_ops:
            items.append(f"  {op}: {'✗' if op in not_available else '✓'}")
    elif "control-plane" in phases_seen:
        items.append("Cluster operators: ⏳")
    return items


def _ocp_build_progress_items(
    phases_seen, cp_names, node_status, full_text, tracked_ops, op_aliases, _re
):
    """Build the progress items list from detected phases and node status."""
    items = _build_early_phase_items(phases_seen, node_status, cp_names)
    items.extend(_build_bootstrap_items(phases_seen, node_status, full_text, _re))
    not_available = _parse_unavailable_operators(full_text, tracked_ops, op_aliases)
    items.extend(_build_operator_items(phases_seen, tracked_ops, not_available))
    return items


def _phase_detail_label(phases_seen):
    """Return a human-readable label for the current install phase."""
    _phase_labels = [
        (("downloading",), ("downloaded",), "downloading OCP tools"),
        (("creating-iso",), ("iso-ready",), "building agent ISO"),
        (("iso-ready",), ("nodes-booted",), "booting nodes from ISO"),
        (("waiting-init",), ("api-init",), "waiting for cluster init"),
        (("api-init",), ("validation",), "validating hosts"),
    ]
    for required, excludes, label in _phase_labels:
        if all(r in phases_seen for r in required) and all(
            e not in phases_seen for e in excludes
        ):
            return label
    return "installing"


def _ocp_build_summary_detail(phases_seen, full_text):
    """Build the summary detail line from detected phases."""
    detail = _phase_detail_label(phases_seen)
    for line in reversed(full_text.split("\n")):
        if "done (" in line:
            msg = line.split("msg=")[-1] if "msg=" in line else line
            detail = msg.strip()
            if len(detail) > 60:
                detail = detail[:57] + "..."
            break
    return detail


def _report_pre_install_status(check, push_fn):
    """Report oc-mirror / registry status while waiting for install.log."""
    parts = check.split("---")
    mirror_running = parts[0].strip() if len(parts) > 0 else ""
    registry_active = parts[1].strip() if len(parts) > 1 else ""
    if "oc-mirror" in mirror_running:
        push_fn("installing", "mirroring OCP images (oc-mirror)")
    elif registry_active == "active":
        push_fn("installing", "setting up disconnected registry")
    else:
        push_fn("installing", "preparing environment")


def _ocp_wait_for_install_log(host, project_id, bastion_ip, password, push_fn):
    """Wait for install.log to appear, reporting oc-mirror / registry activity."""
    import time as _t

    push_fn("installing", "preparing environment")
    pre_install_deadline = _t.time() + 5400
    while _t.time() < pre_install_deadline:
        check = _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "pgrep -af oc-mirror 2>/dev/null; echo '---';"
            " systemctl is-active podman-registry 2>/dev/null; echo '---';"
            " ls /home/*/install.log 2>/dev/null",
            timeout=10,
        )
        if check and "/home/" in check.split("---")[-1]:
            return
        if check:
            _report_pre_install_status(check, push_fn)
        _t.sleep(15)


def _check_install_terminal_state(full_text, push_fn, project_id, start, _t):
    """Check for install completion or failure. Returns (result, elapsed) or None."""
    _failure_markers = [
        "Bootstrap failed to complete",
        "failed to complete",
        "context deadline exceeded",
    ]
    if any(m in full_text for m in _failure_markers):
        push_fn("error", "install failed")
        _ocp_update_status(project_id, "error")
        logger.warning("OCP install failed for %s", project_id[:8])
        return "error", None

    _success_markers = [
        "Install complete!",
        "Install completed",
        "All cluster operators have completed",
    ]
    if any(m in full_text for m in _success_markers):
        items = [
            "Validation: ✓",
            "Bootstrap: ✓",
            "Control plane: ✓",
            "Cluster operators: ✓",
        ]
        push_fn("ready", "install complete", items=items)
        elapsed_secs = int(_t.time() - start)
        push_fn("ready", "cluster ready")
        _ocp_update_status(project_id, "ready", elapsed_secs)
        logger.info("OCP health monitor (install) complete for %s", project_id[:8])
        return "complete", elapsed_secs

    return None


def _ocp_monitor_fresh_install(
    host, project_id, bastion_ip, password, cp_names, push_fn, start
):
    """Monitor fresh OCP install via install.log.

    Returns ('complete', elapsed), ('error', None), or ('timeout', None).
    """
    import time as _t

    _ocp_wait_for_install_log(host, project_id, bastion_ip, password, push_fn)

    # Monitor install.log progress with structured phases
    push_fn("installing", "waiting for OpenShift install")
    install_deadline = _t.time() + 7200
    tracked_ops = [
        "authentication",
        "console",
        "image-registry",
        "ingress",
        "monitoring",
        "openshift-apiserver",
        "openshift-samples",
        "olm-packageserver",
    ]
    op_aliases = {"operator-lifecycle-manager-packageserver": "olm-packageserver"}
    phases_seen = set()

    while _t.time() < install_deadline:
        result = _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "cat /home/cloud-user/install.log 2>/dev/null"
            " || echo 'waiting for install to start'",
            timeout=15,
        )
        if not result:
            _t.sleep(15)
            continue

        full_text = result

        terminal = _check_install_terminal_state(
            full_text, push_fn, project_id, start, _t
        )
        if terminal is not None:
            return terminal

        items, detail, phases_seen, _node_status = _ocp_parse_install_phases(
            full_text, phases_seen, cp_names, tracked_ops, op_aliases
        )
        push_fn("installing", detail, items=items)
        _t.sleep(15)

    push_fn("timeout", "install timed out")
    _ocp_update_status(project_id, "error")
    logger.warning("OCP install timed out for %s", project_id[:8])
    return "timeout", None


def _ocp_wait_for_nodes_ready(
    host, project_id, bastion_ip, password, cp_names, push_fn, deadline
):
    """Phase 3: wait for nodes Ready, approve CSRs. Returns True if all ready."""
    import time as _t

    logger.info("OCP monitor %s: waiting for nodes Ready", project_id[:8])
    push_fn("nodes", "waiting for nodes to be Ready")
    api_seen = False
    last_csr_check = 0.0

    def _do_approve():
        return _approve_pending_csrs(host, project_id, bastion_ip, password)

    while _t.time() < deadline:
        result = _exec_on_bastion(
            host, project_id, bastion_ip, password, _CMD_GET_NODES, timeout=10
        )
        if not _is_api_error(result):
            api_seen = True
            items, ready_count, _total = _parse_node_readiness(result)
            if items:
                push_fn("nodes", f"{ready_count}/{len(cp_names)} ready", items)
                if ready_count >= len(cp_names):
                    return True
        else:
            push_fn("nodes", _MSG_WAITING_API)

        if api_seen:
            last_csr_check = _approve_csrs_if_due(_do_approve, push_fn, last_csr_check)
        _t.sleep(5)

    return False


def _ocp_wait_for_operators(host, project_id, bastion_ip, password, push_fn, deadline):
    """Phase 4: wait for cluster operators. Returns True if all available."""
    import time as _t

    logger.info("OCP monitor %s: waiting for operators", project_id[:8])
    push_fn("operators", "waiting for cluster operators")
    last_csr_check_ops = 0.0

    def _do_approve():
        return _approve_pending_csrs(host, project_id, bastion_ip, password)

    while _t.time() < deadline:
        last_csr_check_ops = _approve_csrs_if_due(
            _do_approve, push_fn, last_csr_check_ops
        )
        result = _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "oc get co --no-headers 2>/dev/null",
            timeout=15,
        )
        if not _is_api_error(result):
            items, available_count, total = _parse_operator_status(result)
            if total > 0:
                push_fn(
                    "operators",
                    f"{available_count}/{total} operators available",
                    items,
                )
                if available_count >= total:
                    return True
        else:
            push_fn("operators", _MSG_WAITING_API)
        _t.sleep(10)

    return False


def _ocp_check_console_route(host, project_id, bastion_ip, password, push_fn):
    """Check if console and OAuth routes are responding. Returns True if both ready."""
    import time as _t

    push_fn("console", "console operator available, verifying route...")
    console_url_result = _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        password,
        "curl -skm 10 -o /dev/null -w '%{http_code}' "
        "https://console-openshift-console.apps."
        "$(oc whoami --show-server 2>/dev/null | sed 's|https://api\\.||;s|:6443||') "
        "2>/dev/null || echo 000",
        timeout=20,
    )
    http_code = (console_url_result or "000").strip()
    if not (
        http_code.startswith("2") or http_code.startswith("3") or http_code == "403"
    ):
        push_fn(
            "console",
            "waiting for console route"
            + (f" (HTTP {http_code})" if http_code not in ("000", "") else ""),
        )
        _t.sleep(10)
        return False

    # Console responds — now verify OAuth route
    oauth_result = _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        password,
        "curl -skm 10 -o /dev/null -w '%{http_code}' "
        "https://oauth-openshift.apps."
        "$(oc whoami --show-server 2>/dev/null | sed 's|https://api\\.||;s|:6443||') "
        "2>/dev/null || echo 000",
        timeout=20,
    )
    oauth_code = (oauth_result or "000").strip()
    if not (
        oauth_code.startswith("2") or oauth_code.startswith("3") or oauth_code == "403"
    ):
        push_fn(
            "console",
            "waiting for OAuth route"
            + (f" (HTTP {oauth_code})" if oauth_code not in ("000", "") else ""),
        )
        _t.sleep(10)
        return False

    push_fn("console", "console and OAuth ready")
    return True


def _ocp_wait_for_console_route(
    host, project_id, bastion_ip, password, push_fn, deadline, topology
):
    """Phase 5: wait for console route. Returns True if console ready."""
    import time as _t

    logger.info("OCP monitor %s: waiting for console", project_id[:8])
    push_fn("console", _MSG_WAITING_CONSOLE)

    # Restart ingress router to pick up fresh certs after pattern deploy
    if _is_pattern_deploy(topology):
        push_fn("console", "restarting ingress router")
        _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "oc rollout restart deployment/router-default"
            " -n openshift-ingress 2>/dev/null || true",
            timeout=15,
        )
        _t.sleep(10)

    last_csr_check_console = 0.0

    def _do_approve():
        return _approve_pending_csrs(host, project_id, bastion_ip, password)

    while _t.time() < deadline:
        last_csr_check_console = _approve_csrs_if_due(
            _do_approve, push_fn, last_csr_check_console
        )
        result = _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "oc get co console --no-headers 2>/dev/null",
            timeout=15,
        )
        if result and "error" not in result.lower():
            parts = result.strip().split()
            co_available = parts[2] == "True" if len(parts) >= 4 else False
            if co_available:
                if _ocp_check_console_route(
                    host, project_id, bastion_ip, password, push_fn
                ):
                    return True
                continue
            push_fn("console", "waiting for console operator")
        push_fn("console", _MSG_WAITING_CONSOLE)
        _t.sleep(5)

    return False


def _ocp_post_pattern_cert_refresh(
    host, project_id, bastion_ip, password, topology, push_fn
):
    """Post-pattern deploy: refresh bastion certs if recert was used."""
    used_recert = False
    for node in topology.get("nodes", []):
        if node.get("type") == "storageNode":
            pid = node.get("data", {}).get("patternId")
            if pid:
                try:
                    from app.core.database import SessionLocal as _SL

                    _db = _SL()
                    pat = _db.query(Pattern).filter_by(id=pid).first()
                    used_recert = bool(pat and pat.recert)
                    _db.close()
                except Exception:
                    pass
                break

    rhcos_count = sum(
        1
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode" and n.get("data", {}).get("os") == "rhcos"
    )
    if not bastion_ip or not (used_recert or rhcos_count == 1):
        return

    push_fn("certs", "refreshing bastion certificates")
    _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        password,
        "export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig; "
        "oc get secret -n openshift-ingress router-certs-default "
        "-o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d "
        "| sudo tee /etc/pki/ca-trust/source/anchors/ocp-ingress.pem >/dev/null "
        "&& sudo update-ca-trust",
        timeout=15,
    )

    push_fn("certs", "verifying bastion setup")

    def _bastion_oc(cmd, timeout=15):
        return _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            f"export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig; {cmd}",
            timeout=timeout,
        )

    _verify_bastion_browser(_bastion_oc, push_fn, project_id)


def _ocp_push_status(project_id, phase, detail, items=None):
    """Push OCP health status via WebSocket and persist to DB."""
    msg = {"type": "ocp-health", "phase": phase, "detail": detail}
    if items:
        msg["items"] = items
    notify_project(project_id, msg)
    try:
        from app.core.database import SessionLocal as _PushSL
        from app.models.project import Project as _PushProj

        _ss = _PushSL()
        _pp = _ss.get(_PushProj, project_id)
        if _pp:
            _pp.ocp_status_detail = detail
            _ss.commit()
        _ss.close()
    except Exception:
        logger.exception("Failed to save ocp_status_detail for %s", project_id[:8])


def _ocp_ping_cp_nodes(
    host, project_id, bastion_ip, password, cp_names, push_fn, deadline
):
    """Phase 2: ping control-plane nodes and approve CSRs while waiting."""
    import time as _t

    push_fn("nodes", "pinging control plane nodes")
    last_csr_check_ping = 0
    while _t.time() < deadline:
        if _t.time() - last_csr_check_ping >= 15:
            approved = _approve_pending_csrs(host, project_id, bastion_ip, password)
            if approved:
                push_fn("certs", f"approved {approved} certificate(s)")
            last_csr_check_ping = _t.time()

        items = []
        all_up = True
        for idx, name in enumerate(cp_names):
            ip_suffix = 10 + idx
            result = _exec_on_bastion(
                host,
                project_id,
                bastion_ip,
                password,
                f"ping -c1 -W2 10.0.0.{ip_suffix} >/dev/null 2>&1 && echo up || echo down",
                timeout=10,
            )
            if result and "up" in result:
                items.append(f"{name}: reachable")
            else:
                items.append(f"{name}: waiting")
                all_up = False
        push_fn(
            "nodes",
            f"{sum(1 for i in items if 'reachable' in i)}/{len(cp_names)} nodes reachable",
            items,
        )
        if all_up:
            break
        _t.sleep(5)


def _ocp_final_csr_sweep(host, project_id, bastion_ip, password, topology, push_fn):
    """Run final CSR approval sweep and post-pattern cert refresh."""
    import time as _t

    for _ in range(6):
        approved = _approve_pending_csrs(host, project_id, bastion_ip, password)
        if not approved:
            break
        push_fn("certs", f"approved {approved} certificate(s)")
        _t.sleep(10)

    if _is_pattern_deploy(topology):
        _ocp_post_pattern_cert_refresh(
            host, project_id, bastion_ip, password, topology, push_fn
        )


def _ocp_report_final_status(
    project_id,
    nodes_ready,
    operators_ready,
    console_ready,
    elapsed_str,
    elapsed_secs,
    push_fn,
):
    """Determine and report final OCP health status."""
    not_ready = [
        label
        for label, ready in [
            ("nodes", nodes_ready),
            ("operators", operators_ready),
            ("console", console_ready),
        ]
        if not ready
    ]
    if not_ready:
        detail = f"timed out waiting for: {', '.join(not_ready)}"
        push_fn("warning", detail)
        logger.warning(
            "OCP health monitor %s: %s (%s)", project_id[:8], detail, elapsed_str
        )
        _ocp_update_status(project_id, "warning", elapsed_secs)
    else:
        push_fn("ready", "cluster ready")
        logger.info(
            "OCP health monitor complete for %s (%s)", project_id[:8], elapsed_str
        )
        _ocp_update_status(project_id, "ready", elapsed_secs)


def _ocp_health_inner(project_id, host_id, topology, deploy_start, _mon_db):
    import time as _t

    from app.models.host import Host as _Host2

    host = _mon_db.query(_Host2).filter_by(id=host_id).first()
    if not host:
        return

    start = deploy_start or _t.time()

    def _elapsed():
        s = int(_t.time() - start)
        return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"

    def _push(phase, detail, items=None):
        _ocp_push_status(project_id, phase, f"{detail} ({_elapsed()})", items)

    # Extract topology info
    bastion, bastion_ip, password, cp_names, dns_domain = _ocp_extract_topology_info(
        topology
    )

    deadline = _t.time() + 1800
    logger.info(
        "OCP health monitor started for %s (bastion=%s, domain=%s)",
        project_id[:8],
        bastion_ip,
        dns_domain,
    )

    # Phase 1: Wait for OCP access (bastion SSH or direct oc)
    if bastion:
        if not _ocp_wait_for_bastion_ssh(
            host, project_id, bastion, bastion_ip, password, _push, deadline
        ):
            return
        logger.info(
            "OCP monitor %s: bastion SSH ready (%s)", project_id[:8], _elapsed()
        )
    elif not _ocp_wait_for_direct_oc(host, project_id, _push, deadline):
        return

    # Fresh install path (non-pattern with bastion) — monitor and return
    is_pattern = _is_pattern_deploy(topology)
    if not is_pattern and bastion:
        _ocp_monitor_fresh_install(
            host, project_id, bastion_ip, password, cp_names, _push, start
        )
        return

    # Phase 2: Ping CP nodes (pattern deploy path)
    if bastion:
        _ocp_ping_cp_nodes(
            host, project_id, bastion_ip, password, cp_names, _push, deadline
        )

    logger.info("OCP monitor %s: nodes reachable (%s)", project_id[:8], _elapsed())

    # Force kube-apiserver rollout to pick up current kubelet serving CA
    _push("certs", "refreshing API server certificates")
    _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        password,
        'oc patch kubeapiserver cluster --type=merge -p \'{"spec":{"forceRedeploymentReason":"troshka-cert-refresh-\'$(date +%s)\'"}}\' 2>/dev/null',
        timeout=10,
    )
    logger.info("Triggered kube-apiserver rollout for %s", project_id[:8])

    # Phase 3-5: nodes Ready, operators, console
    nodes_ready = _ocp_wait_for_nodes_ready(
        host, project_id, bastion_ip, password, cp_names, _push, deadline
    )
    operators_ready = _ocp_wait_for_operators(
        host, project_id, bastion_ip, password, _push, deadline
    )
    console_ready = _ocp_wait_for_console_route(
        host, project_id, bastion_ip, password, _push, deadline, topology
    )

    # Final CSR sweep + post-pattern cert refresh
    _ocp_final_csr_sweep(host, project_id, bastion_ip, password, topology, _push)

    # Final status
    elapsed_secs = int(_t.time() - start)
    _ocp_report_final_status(
        project_id,
        nodes_ready,
        operators_ready,
        console_ready,
        _elapsed(),
        elapsed_secs,
        _push,
    )


def stop_project_async(project_id: str):
    """Background thread: stop a project's VMs and tear down networks."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project:
            return

        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host:
            error_msg = "Host is disconnected or unavailable — cannot stop VMs"
            project.state = "error"
            project.deploy_error = error_msg
            s.commit()
            notify_project(
                project_id,
                {
                    "type": "project-state",
                    "state": "error",
                    "deploy_error": error_msg,
                },
            )
            return

        topology = project.topology or {}
        vms = _extract_vms(topology)

        # KubeVirt native: patch VM running state via K8s API
        if host.host_type == "kubevirt-cluster":
            from app.models.provider import Provider
            from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

            provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if provider:
                custom_api, _, _ = _get_k8s_clients(provider)
                namespace = _project_ns(provider, project_id)
                for vm in vms:
                    kv_name = f"troshka-vm-{vm['node_id'][:8]}"
                    try:
                        custom_api.patch_namespaced_custom_object(
                            group="kubevirt.io",
                            version="v1",
                            namespace=namespace,
                            plural="virtualmachines",
                            name=kv_name,
                            body={"spec": {"running": False}},
                        )
                    except Exception as e:
                        logger.warning(
                            "Stop %s: failed to stop KubeVirt VM %s: %s",
                            project_id[:8],
                            kv_name,
                            e,
                        )
        else:
            if not host.ip_address:
                error_msg = "Host is disconnected or unavailable — cannot stop VMs"
                project.state = "error"
                project.deploy_error = error_msg
                s.commit()
                notify_project(
                    project_id,
                    {
                        "type": "project-state",
                        "state": "error",
                        "deploy_error": error_msg,
                    },
                )
                return

            # Stop VMs via troshkad
            for vm in vms:
                vm_name = _vm_domain_name(project_id, vm["node_id"])
                try:
                    job_id = start_job(host, "/vms/stop", {"domain_name": vm_name})
                    wait_for_job(host, job_id, timeout=90)
                except TroshkadError as e:
                    logger.warning(
                        "Stop %s: failed to stop %s: %s", project_id[:8], vm_name, e
                    )

        # BMC, networks, and EIPs stay intact on stop — only torn down on delete
        project.state = "stopped"
        project.deploy_error = None

        # Clear auto-stop timer (consumed; will restart on next start)
        project.auto_stop_started_at = None
        project.auto_stop_expires_at = None
        project.auto_stop_warned = False

        s.commit()
        notify_project(
            project_id,
            {
                "type": "project-state",
                "state": "stopped",
                "deploy_error": None,
                "auto_stopped": project.auto_stopped,
                "auto_stop_expires_at": None,
                "lifetime_expires_at": (
                    project.lifetime_expires_at.isoformat()
                    if project.lifetime_expires_at
                    else None
                ),
            },
        )
        logger.info("Stop %s: complete", project_id[:8])

    except Exception:
        logger.exception("Stop %s failed", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Stop failed unexpectedly. Check server logs."
                s.commit()
                notify_project(
                    project_id,
                    {
                        "type": "project-state",
                        "state": "error",
                        "deploy_error": project.deploy_error,
                    },
                )
        except Exception:
            pass
    finally:
        s.close()


def start_project_async(project_id: str):
    """Background thread: restart a stopped project."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project:
            return

        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host:
            error_msg = "Host is disconnected or unavailable — cannot start VMs"
            project.state = "error"
            project.deploy_error = error_msg
            s.commit()
            notify_project(
                project_id,
                {
                    "type": "project-state",
                    "state": "error",
                    "deploy_error": error_msg,
                },
            )
            return

        # KubeVirt native: just patch VMs to running, no EIPs/networks/PXE
        if host.host_type == "kubevirt-cluster":
            from app.models.provider import Provider
            from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

            provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if provider:
                custom_api, _, _ = _get_k8s_clients(provider)
                namespace = _project_ns(provider, project_id)
                topology = project.topology or {}
                vms = _extract_vms(topology)
                for vm in vms:
                    kv_name = f"troshka-vm-{vm['node_id'][:8]}"
                    try:
                        custom_api.patch_namespaced_custom_object(
                            group="kubevirt.io",
                            version="v1",
                            namespace=namespace,
                            plural="virtualmachines",
                            name=kv_name,
                            body={"spec": {"running": True}},
                        )
                    except Exception as e:
                        logger.warning(
                            "Start %s: failed to start KubeVirt VM %s: %s",
                            project_id[:8],
                            kv_name,
                            e,
                        )

            project.state = "active"
            project.deploy_error = None
            project.auto_stopped = False

            if project.auto_stop_minutes:
                now = datetime.datetime.now(datetime.UTC)
                project.auto_stop_started_at = now
                project.auto_stop_expires_at = now + datetime.timedelta(
                    minutes=project.auto_stop_minutes
                )
                project.auto_stop_warned = False

            s.commit()
            notify_project(
                project_id,
                {
                    "type": "project-state",
                    "state": "active",
                    "deploy_error": None,
                    "auto_stop_expires_at": (
                        project.auto_stop_expires_at.isoformat()
                        if project.auto_stop_expires_at
                        else None
                    ),
                    "lifetime_expires_at": (
                        project.lifetime_expires_at.isoformat()
                        if project.lifetime_expires_at
                        else None
                    ),
                },
            )
            logger.info("Start %s: kubevirt VMs started", project_id[:8])
            return

        if not host.ip_address:
            error_msg = "Host is disconnected or unavailable — cannot start VMs"
            project.state = "error"
            project.deploy_error = error_msg
            s.commit()
            notify_project(
                project_id,
                {
                    "type": "project-state",
                    "state": "error",
                    "deploy_error": error_msg,
                },
            )
            return

        topology = project.topology or {}
        vni_map = project.vni_map or {}

        # Re-associate EIPs first so topology has _private_ip for DNAT rules
        from app.models.elastic_ip import ElasticIp
        from app.services.eip_service import associate_eip

        project_eips = (
            s.query(ElasticIp).filter_by(project_id=project_id, state="allocated").all()
        )
        for eip in project_eips:
            try:
                associate_eip(s, eip, host)
                for ext_ip in (topology or {}).get("externalIps", []):
                    if ext_ip.get("id") == eip.canvas_eip_id:
                        ext_ip["_private_ip"] = eip.private_ip
                        ext_ip["ip"] = eip.public_ip
            except Exception:
                logger.warning("Failed to re-associate EIP %s on start", eip.public_ip)

        if project_eips:
            import json

            from sqlalchemy import text

            s.execute(
                text("UPDATE projects SET topology = :topo WHERE id = :pid"),
                {"topo": json.dumps(topology), "pid": project_id},
            )
            s.commit()
            s.refresh(project)
            topology = project.topology or {}

            from app.models.provider import Provider
            from app.services.eip_service import sync_security_group_rules

            provider = (
                s.query(Provider).filter_by(id=project.provider_id).first()
                if project.provider_id
                else None
            )
            if not provider and host.provider_id:
                provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if provider:
                gw_node = next(
                    (
                        n
                        for n in (topology or {}).get("nodes", [])
                        if n.get("type") == "networkNode"
                        and n.get("data", {}).get("subtype") == "gateway"
                        and n.get("data", {}).get("gatewayMode") == "nat-portforward"
                    ),
                    None,
                )
                if gw_node:
                    desired_sg = [
                        {
                            "project_id": project_id,
                            "ext_port": int(pf["extPort"]),
                            "protocol": "tcp",
                        }
                        for pf in gw_node.get("data", {}).get("portForwards", [])
                        if pf.get("extPort")
                    ]
                    sync_security_group_rules(s, provider, desired_sg)

        # Recreate networks via troshkad (serialized to avoid nftables contention)
        if vni_map:
            with _get_network_lock(host.id):
                net_result = _setup_networks_via_troshkad(
                    host, topology, vni_map, s, project_id
                )
            if net_result is not True:
                project.state = "error"
                project.deploy_error = f"Network setup failed on restart: {net_result}"
                s.commit()
                return

        # Re-cache any missing library images (ISOs, base disks)
        cache_library_images(topology, host, s)

        # Re-start PXE boot services if needed
        _setup_pxe_via_troshkad(host, topology, vni_map, project_id)

        # Start VMs via troshkad
        start_failures = _start_vms_via_troshkad(host, project_id, topology)

        if start_failures:
            failed_names = ", ".join(name for name, _ in start_failures)
            error_msg = f"Failed to start VMs: {failed_names}"
            logger.error("Start %s: %s", project_id[:8], error_msg)
            project.state = "error"
            project.deploy_error = error_msg
            s.commit()
            notify_project(
                project_id,
                {"type": "project-state", "state": "error", "deploy_error": error_msg},
            )
            return

        # Re-start BMC endpoints
        bmc_config = _extract_bmc_config(topology, project_id)
        if bmc_config:
            logger.info("Start %s: re-starting BMC endpoints", project_id[:8])
            try:
                _setup_bmc_via_troshkad(host, project_id, bmc_config)
            except Exception:
                logger.warning("Start %s: BMC setup failed (non-fatal)", project_id[:8])

        project.state = "active"
        project.deploy_error = None
        project.auto_stopped = False

        # Restart auto-stop timer
        if project.auto_stop_minutes:
            now = datetime.datetime.now(datetime.UTC)
            project.auto_stop_started_at = now
            project.auto_stop_expires_at = now + datetime.timedelta(
                minutes=project.auto_stop_minutes
            )
            project.auto_stop_warned = False

        if _has_ocp_monitor(topology):
            project.ocp_status = "monitoring"
            project.ocp_status_detail = None
            project.ocp_install_elapsed = None
            project.ocp_monitor_started_at = datetime.datetime.now(datetime.UTC)
        s.commit()
        notify_project(
            project_id,
            {
                "type": "project-state",
                "state": "active",
                "deploy_error": None,
                "auto_stop_expires_at": (
                    project.auto_stop_expires_at.isoformat()
                    if project.auto_stop_expires_at
                    else None
                ),
                "lifetime_expires_at": (
                    project.lifetime_expires_at.isoformat()
                    if project.lifetime_expires_at
                    else None
                ),
            },
        )
        logger.info("Start %s: complete", project_id[:8])

    except Exception:
        logger.exception("Start %s failed", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Start failed unexpectedly. Check server logs."
                s.commit()
                notify_project(
                    project_id,
                    {
                        "type": "project-state",
                        "state": "error",
                        "deploy_error": project.deploy_error,
                    },
                )
        except Exception:
            pass
    finally:
        s.close()


def _delete_project_record(project_id: str):
    from app.core.database import SessionLocal
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.get(Project, project_id)
        if project:
            s.delete(project)
            s.commit()
            notify_project(project_id, {"type": "project-deleted"})
            logger.info("Destroy %s: DB record deleted", project_id[:8])
    finally:
        s.close()


def _set_destroy_error(project_id: str, error: str):
    from app.core.database import SessionLocal
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.get(Project, project_id)
        if project:
            project.state = "error"
            project.deploy_error = f"Delete failed: {error}"
            s.commit()
            notify_project(
                project_id,
                {
                    "type": "project-state",
                    "state": "error",
                    "deploy_error": project.deploy_error,
                },
            )
    finally:
        s.close()


def destroy_project_sync(ctx: dict, *, delete_record: bool = True):
    """Synchronously destroy a project's VMs and networks."""
    _deploy_semaphore.acquire()
    try:
        _destroy_project_inner(ctx, delete_record=delete_record)
    finally:
        _deploy_semaphore.release()


def _destroy_kubevirt_native(project_id, host, session, delete_record):
    """Destroy a project via KubeVirt operator and wait for namespace cleanup."""
    from app.models.provider import Provider
    from app.services.providers import get_provider_driver

    provider = session.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        if delete_record:
            _delete_project_record(project_id)
        return

    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import release_eip

    project_eips = session.query(ElasticIp).filter_by(project_id=project_id).all()
    for eip in project_eips:
        try:
            release_eip(session, eip)
        except Exception:
            logger.warning(
                "Destroy %s: failed to release EIP %s", project_id[:8], eip.public_ip
            )

    driver = get_provider_driver(provider)
    try:
        driver.destroy_project(provider, project_id)
        logger.info("Destroy %s: kubevirt project deleted", project_id[:8])
    except Exception as e:
        logger.exception("Destroy %s: kubevirt cleanup failed", project_id[:8])
        _set_destroy_error(project_id, str(e))
        return

    import time as _del_time

    from kubernetes.client.exceptions import ApiException as _KApiErr

    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    _, core_api, _ = _get_k8s_clients(provider)
    ns_name = _project_ns(provider, project_id)
    for _ in range(60):
        try:
            core_api.read_namespace(name=ns_name)
            _del_time.sleep(5)
        except _KApiErr as e:
            if e.status == 404:
                break
            _del_time.sleep(5)
        except Exception:
            break
    logger.info("Destroy %s: namespace cleanup complete", project_id[:8])
    if delete_record:
        _delete_project_record(project_id)


def _destroy_container(host, project_id, ctr, topo, pool):
    """Destroy a single container or pod via troshkad."""
    volumes = _find_container_volumes(ctr["node_id"], topo, project_id, pool)
    vol_dicts = [{"mount_dir": v["mount_dir"]} for v in volumes]
    if ctr.get("is_pod"):
        name = f"troshka-{project_id[:8]}-{ctr['name']}"
        endpoint = "/pods/destroy"
        payload = {"pod_name": name, "project_id": project_id, "volumes": vol_dicts}
    else:
        name = f"troshka-{project_id[:8]}-{ctr['node_id'][:8]}"
        endpoint = "/containers/destroy"
        payload = {
            "container_name": name,
            "project_id": project_id,
            "volumes": vol_dicts,
        }
    try:
        job_id = start_job(host, endpoint, payload)
        wait_for_job(host, job_id, timeout=30)
    except TroshkadError as e:
        label = "pod" if ctr.get("is_pod") else "container"
        logger.warning(
            "Destroy %s: failed to destroy %s %s: %s", project_id[:8], label, name, e
        )


def _destroy_troshkad_resources(host, project_id, topo, vni_map, session):
    """Tear down containers, VMs, files, metadata, BMC, and networks via troshkad."""
    # Destroy containers first (before networks teardown)
    pool = _get_host_pool(host, session)
    containers = _extract_containers(topo)
    for ctr in containers:
        _destroy_container(host, project_id, ctr, topo, pool)

    # Destroy VMs via troshkad
    vms = _extract_vms(topo)
    for vm in vms:
        vm_name = _vm_domain_name(project_id, vm["node_id"])
        try:
            job_id = start_job(host, "/vms/destroy", {"domain_name": vm_name})
            wait_for_job(host, job_id, timeout=60)
        except TroshkadError as e:
            logger.warning(
                "Destroy %s: failed to destroy %s: %s", project_id[:8], vm_name, e
            )

    # Remove project VM directory
    pool = _get_host_pool(host, session)
    vm_dir = _vm_dir(project_id, pool)
    paths_to_remove = [vm_dir]
    if pool and pool.mode.startswith("shared"):
        paths_to_remove.append(f"/var/lib/troshka/seeds/{project_id}")
    try:
        job_id = start_job(host, "/files/remove", {"paths": paths_to_remove})
        wait_for_job(host, job_id, timeout=30)
    except TroshkadError as e:
        logger.warning("Destroy %s: failed to remove VM dir: %s", project_id[:8], e)

    # Kill metadata service and remove script/log
    try:
        job_id = start_job(
            host,
            "/files/remove",
            {
                "paths": [
                    f"/opt/troshka/metadata-{project_id[:8]}.py",
                    f"/var/log/troshka-metadata-{project_id[:8]}.log",
                ],
                "kill_pattern": f"metadata-{project_id[:8]}.py",
            },
        )
        wait_for_job(host, job_id, timeout=15)
    except TroshkadError:
        pass

    # Tear down BMC endpoints (sushy-emulator, vbmcd)
    try:
        _teardown_bmc_via_troshkad(host, project_id)
    except Exception as e:
        logger.warning(
            "Destroy %s: BMC teardown failed (non-fatal): %s", project_id[:8], e
        )

    # Tear down networks via troshkad (serialized to avoid nftables contention)
    with _get_network_lock(host.id):
        _teardown_networks_via_troshkad(host, project_id, vni_map)

    from app.services.placement import sync_host_capacity

    sync_host_capacity(session, host)
    session.commit()


def _destroy_cleanup_sg_rules(host, project_id, session):
    """Clean up AWS security group rules tagged with this project's ID."""
    try:
        from app.models.provider import Provider
        from app.services.provider_gc_service import _get_ec2_client

        provider = (
            session.query(Provider).filter_by(id=host.provider_id).first()
            if host.provider_id
            else None
        )
        if not provider or not provider.security_group_id:
            return
        ec2 = _get_ec2_client(provider)
        sg = ec2.describe_security_groups(GroupIds=[provider.security_group_id])
        for perm in sg["SecurityGroups"][0]["IpPermissions"]:
            for ip_range in perm.get("IpRanges", []):
                desc = ip_range.get("Description", "")
                if desc.startswith(f"troshka-pf:{project_id}:"):
                    try:
                        ec2.revoke_security_group_ingress(
                            GroupId=provider.security_group_id,
                            IpPermissions=[
                                {
                                    "IpProtocol": perm["IpProtocol"],
                                    "FromPort": perm["FromPort"],
                                    "ToPort": perm["ToPort"],
                                    "IpRanges": [
                                        {
                                            "CidrIp": "0.0.0.0/0",
                                            "Description": desc,
                                        }
                                    ],
                                }
                            ],
                        )
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(
            "Destroy %s: SG cleanup failed (non-fatal): %s", project_id[:8], e
        )


def _destroy_cleanup_route_access(host, project_id, session):
    """Clean up OCP Route-based external access (OCP Virt only)."""
    try:
        from app.models.provider import Provider
        from app.services.providers import get_provider_driver

        if not host or not host.provider_id:
            return
        provider = session.query(Provider).filter_by(id=host.provider_id).first()
        if not provider or provider.type != "ocpvirt":
            return
        driver = get_provider_driver(provider)
        driver.delete_route_access(provider, project_id)
        logger.info("Destroy %s: cleaned up Route access resources", project_id[:8])
    except Exception:
        logger.warning(
            "Destroy %s: Route cleanup failed (non-fatal)",
            project_id[:8],
            exc_info=True,
        )


def _destroy_project_inner(ctx: dict, *, delete_record: bool = True):
    """Orchestrate project destruction by delegating to focused helper functions."""
    from app.core.database import SessionLocal
    from app.models.host import Host

    project_id = ctx["project_id"]
    s = SessionLocal()
    try:
        host = s.query(Host).filter_by(id=ctx["host_id"]).first()
        if not host or not host.ip_address:
            if delete_record:
                _delete_project_record(project_id)
            return

        # KubeVirt native: delegate destroy to operator
        if host.host_type == "kubevirt-cluster":
            _destroy_kubevirt_native(project_id, host, s, delete_record)
            return

        vni_map = ctx.get("vni_map", {})
        topo = ctx.get("topology", {})

        # Tear down all troshkad-managed resources (containers, VMs, files, BMC, networks)
        _destroy_troshkad_resources(host, project_id, topo, vni_map, s)

        # Delete DNS records if configured
        if ctx.get("dns_provider_id"):
            from app.models.dns_provider import DnsProvider
            from app.services.dns_service import delete_dns_records

            dns_provider = (
                s.query(DnsProvider).filter_by(id=ctx["dns_provider_id"]).first()
            )
            dns_records = topo.get("_dns_records", [])
            if dns_provider and dns_records:
                logger.info("Teardown %s: deleting DNS records", project_id[:8])
                delete_dns_records(dns_provider.type, dns_provider.config, dns_records)

        # Clean up security group rules for this project
        _destroy_cleanup_sg_rules(host, project_id, s)

        # Clean up Route-based external access (OCP Virt only)
        _destroy_cleanup_route_access(host, project_id, s)

        # Release all EIPs for this project
        from app.models.elastic_ip import ElasticIp
        from app.services.eip_service import release_eip

        project_eips = s.query(ElasticIp).filter_by(project_id=project_id).all()
        for eip in project_eips:
            try:
                release_eip(s, eip)
            except Exception:
                logger.warning("Failed to release EIP %s on destroy", eip.public_ip)

        logger.info("Destroy %s: complete, released capacity", project_id[:8])
        s.close()
        if delete_record:
            _delete_project_record(project_id)
        return
    except Exception as e:
        logger.exception("Destroy %s failed", project_id[:8])
        _set_destroy_error(project_id, str(e))
    finally:
        s.close()
