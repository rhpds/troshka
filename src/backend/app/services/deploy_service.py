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

from app.models.host import Host
from app.models.pattern import Pattern
from app.services.troshkad_client import (
    TroshkadError,
    start_job,
    wait_for_job,
)
from app.services.ws_pubsub import notify_project

logger = logging.getLogger(__name__)

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

_active_health_monitors: set = set()
_deploy_progress: dict[str, dict] = {}


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
    _deploy_progress[project_id] = progress
    notify_project(project_id, {"type": "deploy-progress", "progress": progress})


def get_deploy_progress(project_id: str) -> dict | None:
    """Get deploy progress — in-memory first, fall back to DB."""
    if project_id in _deploy_progress:
        return _deploy_progress[project_id]
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
        progress = _deploy_progress.get(project_id)
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


# Serializes nftables-touching network setup across concurrent deploys
_network_lock = threading.Lock()


# ── Shared storage pool helpers ──


def _get_host_pool(host, db_session):
    """Get the storage pool for a host, if any."""
    if not host.storage_pool_id:
        return None
    from app.models.storage_pool import StoragePool

    return db_session.query(StoragePool).get(host.storage_pool_id)


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

    for nid, node in cur_nodes.items():
        if nid in dep_nodes and node.get("type") == "vmNode":
            cur_data = node.get("data", {})
            dep_data = dep_nodes[nid].get("data", {})
            if (
                cur_data.get("vcpus") != dep_data.get("vcpus")
                or cur_data.get("ram") != dep_data.get("ram")
                or cur_data.get("bootDevices") != dep_data.get("bootDevices")
            ):
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
    sum(ic["expected_size"] for ic in items_to_cache)
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


def _deploy_kubevirt_native(project_id, project, host, topology, db):
    """Deploy via KubeVirt operator — create TroshkaProject CR and poll status."""
    import time

    from app.models.provider import Provider
    from app.services.providers import get_provider_driver
    from app.services.s3_storage import _get_s3_config
    from app.services.ws_pubsub import notify_project

    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        project.state = "error"
        project.deploy_error = "No provider found for kubevirt host"
        db.commit()
        return

    driver = get_provider_driver(provider)

    s3_config = _get_s3_config()

    import boto3

    s3_client = boto3.client(
        "s3",
        region_name=s3_config.get("region", "us-east-1"),
        aws_access_key_id=s3_config.get("access_key_id", ""),
        aws_secret_access_key=s3_config.get("secret_access_key", ""),
        endpoint_url=s3_config.get("endpoint_url") or None,
    )
    bucket = s3_config.get("bucket", "troshka-images")

    def _presign(s3_path):
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": s3_path},
            ExpiresIn=86400,
        )

    def _qcow2_virtual_size_gb(s3_path):
        try:
            url = _presign(s3_path)
            import struct
            import urllib.request

            req = urllib.request.Request(url, headers={"Range": "bytes=0-31"})
            resp = urllib.request.urlopen(req, timeout=10)
            header = resp.read()
            if len(header) >= 32 and header[:4] == b"QFI\xfb":
                vsize = struct.unpack(">Q", header[24:32])[0]
                return int(vsize / (1024**3)) + 1
        except Exception:
            pass
        return 0

    pattern_disk_map = {}
    pattern_ids_seen = set()
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") == "storageNode" and data.get("source") == "pattern":
            pid = data.get("patternId", "")
            if pid:
                pattern_ids_seen.add(pid)

    if pattern_ids_seen:
        from app.models.pattern import Pattern as PatternModel

        for pid in pattern_ids_seen:
            pat = db.query(PatternModel).filter_by(id=pid).first()
            if pat and pat.topology:
                for pn in pat.topology.get("nodes", []):
                    pd = pn.get("data", {})
                    if pn.get("type") == "storageNode":
                        orig_id = pd.get("id", pn.get("id", ""))
                        label = pd.get("label", "")
                        pattern_disk_map[(pid, label)] = orig_id

    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") == "storageNode":
            if data.get("source") == "pattern" and data.get("patternId"):
                pid = data["patternId"]
                label = data.get("label", "")
                orig_disk_id = pattern_disk_map.get(
                    (pid, label), data.get("patternDiskId", "")
                )
                s3_path = f"patterns/{pid}/{orig_disk_id}.qcow2"
                data["presignedUrl"] = _presign(s3_path)
                data["resolvedS3Path"] = s3_path
                real_size = _qcow2_virtual_size_gb(s3_path)
                if real_size and real_size > (data.get("size", 0) or 0):
                    data["size"] = real_size
            elif data.get("source") == "library" and data.get("libraryItemId"):
                from app.models.library import LibraryItem

                lib_item = (
                    db.query(LibraryItem).filter_by(id=data["libraryItemId"]).first()
                )
                if lib_item and lib_item.s3_key:
                    s3_path = lib_item.s3_key
                else:
                    fmt = data.get("format", "qcow2")
                    s3_path = f"library/{data['libraryItemId']}.{fmt}"
                data["presignedUrl"] = _presign(s3_path)
                data["resolvedS3Path"] = s3_path

    # Generate per-deploy SSH key pair for exec pod → VM access
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

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
        ssh_keys = data.get("ciSshKeys", [])
        if exec_pubkey not in ssh_keys:
            ssh_keys.append(exec_pubkey)
            data["ciSshKeys"] = ssh_keys
        data["ciUserData"] = generate_userdata(data)

    _update_deploy_progress(project_id, "networks", "creating operator resources")
    notify_project(
        project_id,
        {
            "type": "deploy-progress",
            "step": "networks",
            "detail": "creating operator resources",
        },
    )

    existing_cr = None
    try:
        existing_cr = driver.get_project_status(provider, project_id)
    except Exception:
        pass

    if existing_cr and existing_cr.get("phase"):
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
        _time.sleep(3)

    logger.info(
        "Deploy %s: creating CR with exec_ssh_key=%s",
        project_id[:8],
        bool(exec_privkey_pem),
    )
    try:
        cr_name = driver.deploy_project(
            provider,
            project_id,
            topology,
            s3_config,
            exec_ssh_key=exec_privkey_pem,
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

    deploy_deadline = _time.time() + 7200
    for attempt in range(1440):
        if _time.time() > deploy_deadline:
            break
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

        dv_lines = []
        try:
            from app.services.providers.kubevirt import _get_k8s_clients, _project_ns
            import hashlib

            golden_name_map = {}
            for node in topology.get("nodes", []):
                ndata = node.get("data", {})
                if node.get("type") == "storageNode" and ndata.get("resolvedS3Path"):
                    h = hashlib.sha256(ndata["resolvedS3Path"].encode()).hexdigest()[
                        :16
                    ]
                    golden_name_map[f"golden-{h}"] = ndata.get(
                        "label", ndata.get("name", "")
                    )

            custom_api, _, _ = _get_k8s_clients(provider)
            proj_ns = _project_ns(provider, project_id)
            all_dvs = []
            for ns in ["troshka-cache", proj_ns]:
                try:
                    dvs = custom_api.list_namespaced_custom_object(
                        group="cdi.kubevirt.io",
                        version="v1beta1",
                        namespace=ns,
                        plural="datavolumes",
                    )
                    all_dvs.extend(dvs.get("items", []))
                except Exception:
                    pass
            clone_name_map = {}
            for node in topology.get("nodes", []):
                ndata2 = node.get("data", {})
                if node.get("type") == "storageNode":
                    sid = ndata2.get("id", node.get("id", ""))[:8]
                    label = ndata2.get("label", ndata2.get("name", ""))
                    fmt = ndata2.get("format", "qcow2")
                    for edge in topology.get("edges", []):
                        if edge.get("source") == ndata2.get("id", node.get("id", "")):
                            vm_id = edge.get("target", "")[:8]
                            clone_name_map[f"vm-{vm_id}-disk-{sid}"] = label
                            if fmt == "iso":
                                clone_name_map[f"vm-{vm_id}-cdrom"] = label
                        elif edge.get("target") == ndata2.get("id", node.get("id", "")):
                            vm_id = edge.get("source", "")[:8]
                            clone_name_map[f"vm-{vm_id}-disk-{sid}"] = label
                            if fmt == "iso":
                                clone_name_map[f"vm-{vm_id}-cdrom"] = label

            cache_lines = []
            clone_lines = []
            for dv in all_dvs:
                dv_phase = dv.get("status", {}).get("phase", "")
                dv_progress = dv.get("status", {}).get("progress", "N/A")
                raw_name = dv["metadata"]["name"]
                ns = dv["metadata"]["namespace"]
                friendly = (
                    golden_name_map.get(raw_name)
                    or clone_name_map.get(raw_name)
                    or raw_name
                )
                friendly = friendly[:24]
                if dv_phase == "Succeeded":
                    line = f"{friendly}: done"
                elif dv_phase == "ImportInProgress":
                    conditions = dv.get("status", {}).get("conditions", [])
                    running_reason = ""
                    running_msg = ""
                    for cond in conditions:
                        if cond.get("type") == "Running":
                            running_reason = cond.get("reason", "")
                            running_msg = cond.get("message", "")
                            break
                    if running_reason == "Completed":
                        line = f"{friendly}: writing to storage"
                    elif running_reason == "Error":
                        line = f"{friendly}: error — {running_msg[:40]}"
                    elif dv_progress and dv_progress != "N/A":
                        try:
                            pct = float(dv_progress.rstrip("%"))
                            if pct >= 99.0:
                                line = f"{friendly}: writing to storage — please wait"
                            else:
                                line = f"{friendly}: downloading {dv_progress}"
                        except ValueError:
                            line = f"{friendly}: downloading {dv_progress}"
                    elif running_reason == "TransferRunning":
                        line = f"{friendly}: downloading starting"
                    else:
                        line = f"{friendly}: starting"
                elif dv_phase in ("CloneInProgress", "CloneScheduled"):
                    line = f"{friendly}: cloning"
                elif dv_phase in ("ImportScheduled", "Pending"):
                    line = f"{friendly}: scheduled"
                elif dv_phase == "Failed":
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
                    line = f"{friendly}: error — {short_err}"
                elif dv_phase:
                    line = f"{friendly}: {dv_phase.lower()}"
                else:
                    line = f"{friendly}: waiting"
                if ns == "troshka-cache":
                    cache_lines.append(line)
                else:
                    clone_lines.append(line)
            best_status = {}
            for line in cache_lines + clone_lines:
                label = line.split(":")[0].strip()
                dv_status = line.split(":", 1)[1].strip() if ":" in line else ""
                prev = best_status.get(label, "")
                rank = {
                    "done": 5,
                    "downloading": 4,
                    "cloning": 3,
                    "scheduled": 2,
                    "waiting": 1,
                }

                def _rank(s):
                    for k, v in rank.items():
                        if k in s:
                            return v
                    return 0

                if _rank(dv_status) >= _rank(prev):
                    best_status[label] = dv_status

            for node in topology.get("nodes", []):
                ndata3 = node.get("data", {})
                if node.get("type") == "storageNode" and ndata3.get("source") in (
                    "pattern",
                    "library",
                ):
                    label = ndata3.get("label", ndata3.get("name", ""))[:24]
                    if label and label not in best_status:
                        best_status[label] = "waiting"

            dv_lines = [f"{k}: {v}" for k, v in best_status.items()]
        except Exception:
            pass

        dv_detail = "\n".join(dv_lines) if dv_lines else ""

        all_disks_done = dv_lines and all(": done" in line for line in dv_lines)
        op_stage = progress.get("stage", "") if progress else ""
        op_detail = progress.get("detail", "") if progress else ""

        last = _deploy_progress.get(project_id, {})
        if all_disks_done and op_stage:
            step = op_stage.lower()
            vm_states = status.get("vmStates", {})
            if vm_states:
                ready = sum(
                    1 for s in vm_states.values() if s in ("Running", "Stopped")
                )
                detail = f"{ready}/{len(vm_states)} VMs ready"
            else:
                detail = op_detail or step
        elif dv_lines:
            step = "images"
            detail = dv_detail
        else:
            step = last.get("step", "") or "deploying"
            detail = last.get("detail", "")
        percent = progress.get("percent", 0) if progress else 0

        if not detail and not dv_lines:
            continue

        new_progress = {
            "step": step,
            "detail": detail,
            "percent": percent,
        }
        if new_progress == last:
            continue
        _deploy_progress[project_id] = new_progress
        notify_project(
            project_id,
            {
                "type": "deploy-progress",
                "step": step,
                "detail": detail,
                "percent": percent,
            },
        )

        if phase == "Running":
            project.state = "active"
            clean_topo = copy.deepcopy(topology)
            for node in clean_topo.get("nodes", []):
                ndata = node.get("data", {})
                ndata.pop("resolvedS3Path", None)
                ndata.pop("presignedUrl", None)
            project.deployed_topology = clean_topo
            project.topology = clean_topo
            project.deploy_error = None
            if _is_ocp_topology(topology):
                project.ocp_status = "monitoring"
            db.commit()
            _deploy_progress.pop(project_id, None)
            notify_project(project_id, {"type": "project-state", "state": "active"})
            logger.info("Deploy %s: kubevirt deploy complete", project_id[:8])
            return

        if phase == "Error":
            error_msg = status.get("message", "Operator reported an error")
            project.state = "error"
            project.deploy_error = error_msg
            db.commit()
            _deploy_progress.pop(project_id, None)
            notify_project(
                project_id,
                {"type": "project-state", "state": "error", "deploy_error": error_msg},
            )
            return

        time.sleep(5)

    project.state = "error"
    project.deploy_error = "Deploy timed out waiting for operator (2 hours)"
    db.commit()
    _deploy_progress.pop(project_id, None)
    notify_project(
        project_id,
        {
            "type": "project-state",
            "state": "error",
            "deploy_error": project.deploy_error,
        },
    )


def deploy_project_async(
    project_id: str, auto_start: bool = True, resume_from: str | None = None
):
    """Background thread: deploy a project's topology to a host."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

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
                _deploy_progress.pop(project_id, None)
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
        with _network_lock:
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
            _deploy_progress.pop(project_id, None)
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
            _deploy_progress.pop(project_id, None)
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
            _deploy_progress.pop(project_id, None)
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
            _deploy_progress.pop(project_id, None)
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
                _deploy_progress.pop(project_id, None)
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
            _deploy_progress.pop(project_id, None)
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
            _deploy_progress.pop(project_id, None)
            return
        if bmc_config:
            _update_deploy_progress(project_id, "bmc", "starting BMC endpoints")
            notify_project(
                project_id,
                {"type": "deploy-progress", "progress": _deploy_progress[project_id]},
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
                _deploy_progress.pop(project_id, None)
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
            _deploy_progress.pop(project_id, None)
            return

        # Step 5: Start VMs (unless auto_start is disabled)
        _checkpoint(s, project_id, "starting")
        if auto_start:
            _update_deploy_progress(project_id, "starting", "VMs")
            notify_project(
                project_id,
                {"type": "deploy-progress", "progress": _deploy_progress[project_id]},
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
                _deploy_progress.pop(project_id, None)
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
        _deploy_progress.pop(project_id, None)
        logger.info("Deploy %s: complete — all VMs running", project_id[:8])

        if auto_start and _is_ocp_topology(topology):
            project.ocp_status = "monitoring"
            s.commit()

    except Exception as e:
        logger.exception("Deploy %s failed unexpectedly", project_id[:8])
        _deploy_progress.pop(project_id, None)
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = str(e)
                # Clean up any "downloading" cache entries this deploy created
                if project.host_id:
                    pool = _get_host_pool(s, project.host_id)
                    if pool and pool.mode.startswith("shared"):
                        from app.models.storage_pool import SharedCacheEntry

                        stale = (
                            s.query(SharedCacheEntry)
                            .filter(
                                SharedCacheEntry.storage_pool_id == pool.id,
                                SharedCacheEntry.status == "downloading",
                            )
                            .all()
                        )
                        for entry in stale:
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
            pass
    finally:
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

    is_sno = len(rhcos_vms) == 1

    if is_sno:
        vm = rhcos_vms[0]
        vm_disks = _find_vm_disks(vm["node_id"], topology)
        boot_disk = next((d for d in vm_disks if d.get("format") == "qcow2"), None)
        if boot_disk:
            disk = _disk_path(
                project_id,
                vm["node_id"],
                boot_disk["node_id"],
                boot_disk["format"],
                pool,
            )
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
            vm_name = vm.get("name", vm["node_id"][:8])
            logger.info(
                "Deploy %s: running recert on SNO disk for %s",
                project_id[:8],
                vm_name,
            )
            try:
                recert_params = {"disk": disk, "extend_expiration": True}
                if bastion_disk_path:
                    recert_params["bastion_disk"] = bastion_disk_path
                if common_password:
                    import bcrypt
                    import secrets as _secrets

                    # OCP 4.22+ requires kubeadmin password >= 23 chars
                    kubeadmin_pw = common_password
                    if len(kubeadmin_pw) < 23:
                        kubeadmin_pw = _secrets.token_urlsafe(24)
                    recert_params["common_password"] = kubeadmin_pw
                    pw_hash = bcrypt.hashpw(
                        kubeadmin_pw.encode(), bcrypt.gensalt(rounds=10)
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
                    if common_password:
                        for n in topology.get("nodes", []):
                            if n["id"] == vm["node_id"]:
                                n.setdefault("data", {})[
                                    "ocpKubeadminPassword"
                                ] = kubeadmin_pw
                                break
                    return
                else:
                    err = job.get("result", {}).get("error", "unknown")
                    if pattern_recert:
                        raise RuntimeError(
                            f"Recert required (pattern has expired certs) but failed: {err}"
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
                        "Recert required (pattern has expired certs) but recert endpoint unavailable"
                    )
                logger.warning(
                    "Deploy %s: recert error for %s — falling back to guestfish",
                    project_id[:8],
                    vm_name,
                    exc_info=True,
                )

    operations = [
        {"action": "rm-rf", "path": "/var/lib/kubelet/pki"},
        {"action": "rm-f", "path": "/var/lib/kubelet/kubeconfig"},
    ]

    for vm in rhcos_vms:
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
    has_bastion = any(
        n.get("data", {}).get("label") == "bastion"
        for n in nodes
        if n.get("type") == "vmNode"
    )
    has_rhcos = any(
        n.get("data", {}).get("os") == "rhcos"
        for n in nodes
        if n.get("type") == "vmNode"
    )
    return has_bastion and has_rhcos


def _is_pattern_deploy(topology: dict) -> bool:
    return any(
        n.get("data", {}).get("patternId")
        for n in topology.get("nodes", [])
        if n.get("type") == "storageNode"
    )


def maybe_start_ocp_health_monitor(project_id: str):
    """Start OCP health monitor if project needs it and one isn't already running."""
    if project_id in _active_health_monitors:
        return
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(id=project_id).first()
        if (
            not project
            or project.ocp_status != "monitoring"
            or project.state != "active"
        ):
            return
        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host:
            return
        if host.host_type != "kubevirt-cluster" and host.agent_status != "connected":
            return
        topo = project.deployed_topology or project.topology or {}
        if not _is_ocp_topology(topo):
            return
        if project.ocp_install_elapsed is not None:
            return
        deploy_start = 0
        _active_health_monitors.add(project_id)
        threading.Thread(
            target=_monitor_ocp_health,
            args=(project_id, host.id, topo, deploy_start),
            daemon=True,
            name=f"ocp-health-{project_id[:8]}",
        ).start()
        logger.info("OCP health monitor started on demand for %s", project_id[:8])
    finally:
        db.close()


def _exec_on_bastion(
    host,
    project_id: str,
    bastion_ip: str,
    password: str,
    command: str,
    timeout: int = 15,
):
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
        from app.models.provider import Provider
        from app.core.database import SessionLocal

        db = SessionLocal()
        try:
            provider = db.query(Provider).filter_by(id=host.provider_id).first()
        finally:
            db.close()
        if not provider:
            return None

        from app.services.providers.kubevirt import (
            _find_exec_pod,
            _get_k8s_clients,
            _project_ns,
        )
        from kubernetes.stream import stream as k8s_stream

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


def _approve_pending_csrs(host, project_id, bastion_ip, password):
    """Approve any pending OCP CSRs on the cluster. Returns count approved."""
    result = _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        password,
        "oc get csr --no-headers 2>/dev/null | grep -c Pending || echo 0",
        timeout=10,
    )
    pending = 0
    if result:
        try:
            pending = int(result.strip())
        except ValueError:
            pass
    if pending > 0:
        _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "oc get csr -o name 2>/dev/null | xargs oc adm certificate approve 2>/dev/null",
            timeout=30,
        )
        logger.info(
            "Approved %d pending CSR(s) for project %s", pending, project_id[:8]
        )
    return pending


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
        _active_health_monitors.discard(project_id)
        _mon_db.close()


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
        msg = {
            "type": "ocp-health",
            "phase": phase,
            "detail": f"{detail} ({_elapsed()})",
        }
        if items:
            msg["items"] = items
        notify_project(project_id, msg)

    nodes = topology.get("nodes", [])
    bastion = next(
        (
            n
            for n in nodes
            if n.get("type") == "vmNode" and n.get("data", {}).get("label") == "bastion"
        ),
        None,
    )
    if not bastion:
        return

    bastion_ip = ""
    for nic in bastion.get("data", {}).get("nics", []):
        if nic.get("ip"):
            bastion_ip = nic["ip"]
            break
    if not bastion_ip:
        bastion_ip = "10.0.0.50"
    password = bastion.get("data", {}).get("ciCloudUserPassword", "")

    cp_nodes = [
        n
        for n in nodes
        if n.get("type") == "vmNode" and n.get("data", {}).get("os") == "rhcos"
    ]
    cp_names = [n.get("data", {}).get("label", n["id"][:8]) for n in cp_nodes]

    dns_domain = "ocp.ocp.local"
    for n in nodes:
        if n.get("type") == "networkNode":
            for rec in n.get("data", {}).get("dnsRecords", []):
                name = rec.get("name", "")
                if name.startswith("api."):
                    dns_domain = name[4:]
                    break

    console_url = f"https://console-openshift-console.apps.{dns_domain}"
    deadline = _t.time() + 900
    logger.info(
        "OCP health monitor started for %s (bastion=%s, domain=%s)",
        project_id[:8],
        bastion_ip,
        dns_domain,
    )

    # Phase 1: Wait for bastion SSH — but skip if bastion VM is stopped
    if host.host_type != "kubevirt-cluster":
        from app.services.troshkad_client import get_vm_state as _get_vm_st

        bastion_dom = _vm_domain_name(project_id, bastion["id"])
        try:
            vm_info = _get_vm_st(host, bastion_dom, timeout=5)
            if vm_info.get("state") in ("shut_off", "shutoff"):
                _push(
                    "waiting",
                    "bastion is powered off — start it to enable OCP monitoring",
                )
                return
        except Exception:
            pass

    _push("ssh", "waiting for bastion")
    while _t.time() < deadline:
        result = _exec_on_bastion(
            host, project_id, bastion_ip, password, "echo ok", timeout=5
        )
        if result and "ok" in result:
            break
        _push("ssh", "waiting for bastion")
        _t.sleep(5)
    else:
        _push("timeout", "bastion SSH not available")
        return

    # Detect mode: pattern deploy (cluster pre-installed) vs fresh install (install-ocp.sh running)
    is_pattern = _is_pattern_deploy(topology)
    if not is_pattern:
        # Pre-install phase: detect oc-mirror / registry setup before install log exists
        _push("installing", "preparing environment")
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
                break
            if check:
                parts = check.split("---")
                mirror_running = parts[0].strip() if len(parts) > 0 else ""
                registry_active = parts[1].strip() if len(parts) > 1 else ""
                if "oc-mirror" in mirror_running:
                    _push("installing", "mirroring OCP images (oc-mirror)")
                elif registry_active == "active":
                    _push("installing", "setting up disconnected registry")
                else:
                    _push("installing", "preparing environment")
            _t.sleep(15)

        # Fresh install — monitor install.log progress with structured phases
        _push("installing", "waiting for OpenShift install")
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
        _op_aliases = {"operator-lifecycle-manager-packageserver": "olm-packageserver"}
        phases_seen = set()

        while _t.time() < install_deadline:
            result = _exec_on_bastion(
                host,
                project_id,
                bastion_ip,
                password,
                "cat /home/cloud-user/install.log 2>/dev/null || echo 'waiting for install to start'",
                timeout=15,
            )
            if not result:
                _t.sleep(15)
                continue

            full_text = result
            # Detect early phases from grep markers
            if "Downloading openshift-install" in full_text:
                phases_seen.add("downloading")
            if "Downloaded openshift-install" in full_text:
                phases_seen.add("downloaded")
            if "Creating agent ISO" in full_text:
                phases_seen.add("creating-iso")
            if "Extracting base ISO" in full_text or "Base ISO obtained" in full_text:
                phases_seen.add("extracting-iso")
            if "Generated ISO at" in full_text or "Agent ISO created" in full_text:
                phases_seen.add("iso-ready")
            if "Booted" in full_text and "from ISO" in full_text:
                phases_seen.add("nodes-booted")
            if "Waiting for cluster install to initialize" in full_text:
                phases_seen.add("waiting-init")
            if "Agent Rest API Initialized" in full_text:
                phases_seen.add("api-init")

            # Detect install failure
            if (
                "Bootstrap failed to complete" in full_text
                or "failed to complete" in full_text
                or "context deadline exceeded" in full_text
            ):
                _push("error", "install failed")
                try:
                    from app.core.database import SessionLocal
                    from app.models.project import Project

                    db = SessionLocal()
                    p = db.query(Project).filter_by(id=project_id).first()
                    if p:
                        p.ocp_status = "error"
                        db.commit()
                    db.close()
                except Exception:
                    pass
                logger.warning(
                    "OCP install failed for %s (%s)",
                    project_id[:8],
                    _elapsed(),
                )
                return

            # Detect phases from log content
            if (
                "Install complete!" in full_text
                or "Install completed" in full_text
                or "All cluster operators have completed" in full_text
            ):
                phases_seen.update(
                    ["validation", "bootstrap", "control-plane", "operators"]
                )
                items = [
                    "Validation: ✓",
                    "Bootstrap: ✓",
                    "Control plane: ✓",
                    "Cluster operators: ✓",
                ]
                _push("ready", "install complete", items=items)
                break

            if "validation:" in full_text:
                phases_seen.add("validating")
            if "preparing-for-installation" in full_text:
                phases_seen.add("validation")
                phases_seen.add("preparing")
            if "Preparing cluster" in full_text:
                phases_seen.add("validation")
                phases_seen.add("preparing")
            if "Bootstrap Kube API Initialized" in full_text:
                phases_seen.add("bootstrap-api")
            if (
                "Bootstrap is complete" in full_text
                or "cluster bootstrap is complete" in full_text
            ):
                phases_seen.add("bootstrap")
            if "Waiting up to" in full_text and "to initialize" in full_text:
                phases_seen.add("bootstrap")
            if "Working towards" in full_text:
                phases_seen.add("control-plane")
            if "Cluster is initialized" in full_text:
                phases_seen.add("initialized")

            # Parse per-node status from log
            node_status = {}
            for l in full_text.split("\n"):
                for cp in cp_names:
                    if f"Host {cp}" in l or f"Host: {cp}" in l or f"Node {cp}" in l:
                        msg = l.split("msg=")[-1] if "msg=" in l else l
                        if "Writing image to disk: 100%" in msg:
                            node_status[cp] = "written"
                        elif "Writing image to disk" in msg:
                            pct = (
                                msg.split("Writing image to disk:")[-1]
                                .strip()
                                .rstrip("%")
                                if ":" in msg
                                else ""
                            )
                            node_status.setdefault(cp, f"writing {pct}%")
                        elif "Rebooting" in msg:
                            node_status[cp] = "rebooting"
                        elif "Waiting for bootkube" in msg:
                            node_status[cp] = "bootkube"
                        elif "Configuring" in msg:
                            node_status[cp] = "configuring"
                        elif "Joined" in msg:
                            node_status[cp] = "joined"
                        elif "Done" in msg or "completing installation" in msg:
                            node_status[cp] = "done"

            items = []
            # Early phases: download, ISO generation, node boot
            if "downloading" in phases_seen:
                items.append(
                    f"Download OCP tools: {'✓' if 'downloaded' in phases_seen else '⏳'}"
                )
            if "creating-iso" in phases_seen or "downloaded" in phases_seen:
                items.append(
                    f"Build agent ISO: {'✓' if 'iso-ready' in phases_seen else '⏳'}"
                )
            if "iso-ready" in phases_seen:
                items.append(
                    f"Boot nodes from ISO: {'✓' if 'nodes-booted' in phases_seen else '⏳'}"
                )
            if "nodes-booted" in phases_seen:
                items.append(
                    f"Cluster init: {'✓' if 'api-init' in phases_seen or 'validation' in phases_seen else '⏳' if 'waiting-init' in phases_seen else '—'}"
                )

            if "validation" in phases_seen:
                items.append("Host validation: ✓")
            elif "validating" in phases_seen:
                items.append("Host validation: ⏳")
            elif "api-init" in phases_seen:
                items.append("Host validation: ⏳")

            if "preparing" in phases_seen:
                has_installing = bool(node_status)
                items.append(
                    f"Preparing for installation: {'✓' if has_installing else '⏳'}"
                )

            if node_status:
                all_done = all(s in ("done", "joined") for s in node_status.values())
                items.append(f"Installing nodes: {'✓' if all_done else '⏳'}")
                for cp in cp_names:
                    s = node_status.get(cp, "—")
                    items.append(f"  {cp}: {s}")

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
            elif "bootstrap-api" in phases_seen:
                items.append("Bootstrap: ⏳")
            elif has_bootkube:
                items.append("Bootstrap: ⏳")
            elif node_status:
                items.append("Bootstrap: —")

            if "bootstrap" in phases_seen and "control-plane" not in phases_seen:
                items.append("API: ⏳")

            if "control-plane" in phases_seen:
                items.append("API: ✓")
                cp_detail = "⏳"
                for l in reversed(full_text.split("\n")):
                    if "Working towards" in l:
                        msg = l.split("msg=")[-1] if "msg=" in l else l
                        import re as _re

                        m = _re.search(r"([\d.]+)", msg)
                        if m:
                            cp_detail = f"OCP {m.group(1)} ⏳"
                        break
                if "initialized" in phases_seen:
                    cp_detail = cp_detail.replace(" ⏳", " ✓")
                items.append(f"Cluster init: {cp_detail}")
            elif "bootstrap" in phases_seen:
                items.append("Cluster init: —")

            # Parse operator status from latest "not available" line
            not_available = set()
            for l in reversed(full_text.split("\n")):
                if (
                    "are not available" in l or "is not available" in l
                ) and "Cluster operator" in l:
                    msg = l.split("msg=")[-1] if "msg=" in l else l
                    for real_name, alias in _op_aliases.items():
                        if real_name in msg:
                            not_available.add(alias)
                    for op in tracked_ops:
                        if op in msg:
                            not_available.add(op)
                    break

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

            # Build summary detail line
            detail = "installing"
            if "downloading" in phases_seen and "downloaded" not in phases_seen:
                detail = "downloading OCP tools"
            elif "creating-iso" in phases_seen and "iso-ready" not in phases_seen:
                detail = "building agent ISO"
            elif "iso-ready" in phases_seen and "nodes-booted" not in phases_seen:
                detail = "booting nodes from ISO"
            elif "waiting-init" in phases_seen and "api-init" not in phases_seen:
                detail = "waiting for cluster init"
            elif "api-init" in phases_seen and "validation" not in phases_seen:
                detail = "validating hosts"
            for l in reversed(full_text.split("\n")):
                if "done (" in l:
                    msg = l.split("msg=")[-1] if "msg=" in l else l
                    detail = msg.strip()
                    if len(detail) > 60:
                        detail = detail[:57] + "..."
                    break

            _push("installing", detail, items=items)
            _t.sleep(15)
        else:
            _push("timeout", "install timed out")
            try:
                from app.core.database import SessionLocal
                from app.models.project import Project

                db = SessionLocal()
                p = db.query(Project).filter_by(id=project_id).first()
                if p:
                    p.ocp_status = "error"
                    db.commit()
                db.close()
            except Exception:
                pass
            logger.warning(
                "OCP install timed out for %s (%s)",
                project_id[:8],
                _elapsed(),
            )
            return
        elapsed_secs = int(_t.time() - start)
        _push("ready", "cluster ready")
        try:
            from app.core.database import SessionLocal
            from app.models.project import Project

            db = SessionLocal()
            p = db.query(Project).filter_by(id=project_id).first()
            if p:
                p.ocp_status = "ready"
                p.ocp_install_elapsed = elapsed_secs
                db.commit()
            db.close()
        except Exception:
            pass
        logger.info(
            "OCP health monitor (install) complete for %s (%s)",
            project_id[:8],
            _elapsed(),
        )
        return

    # Phase 2: Ping CP nodes (pattern deploy path)
    # Also approve CSRs early — bootstrap CSRs arrive while nodes are still
    # booting, and approving them immediately lets the node proceed to
    # requesting its serving cert sooner (otherwise there's a multi-minute gap)
    _push("nodes", "pinging control plane nodes")
    last_csr_check_ping = 0
    while _t.time() < deadline:
        if _t.time() - last_csr_check_ping >= 15:
            approved = _approve_pending_csrs(host, project_id, bastion_ip, password)
            if approved:
                _push("certs", f"approved {approved} certificate(s)")
            last_csr_check_ping = _t.time()

        items = []
        all_up = True
        for name in cp_names:
            ip_suffix = 10 + cp_names.index(name)
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
        _push(
            "nodes",
            f"{sum(1 for i in items if 'reachable' in i)}/{len(cp_names)} reachable",
            items,
        )
        if all_up:
            break
        _t.sleep(5)

    # Force kube-apiserver rollout to pick up current kubelet serving CA.
    # After pattern restore or extended downtime, the API server may not
    # trust the kubelet's serving cert — this triggers a redeploy.
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

    # Phase 3: Wait for nodes Ready (approve expired CSRs along the way)
    _push("nodes", "waiting for nodes to be Ready")
    api_seen = False
    last_csr_check = 0
    while _t.time() < deadline:
        result = _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "oc get nodes --no-headers 2>/dev/null",
            timeout=10,
        )
        if result:
            api_seen = True
            items = []
            ready_count = 0
            for line in result.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2:
                    name, status = parts[0], parts[1]
                    items.append(f"{name}: {status}")
                    if "Ready" in status and "Not" not in status:
                        ready_count += 1
            if items:
                _push("nodes", f"{ready_count}/{len(cp_names)} ready", items)
                if ready_count >= len(cp_names):
                    break
        else:
            _push("nodes", "waiting for API server")

        if api_seen and _t.time() - last_csr_check >= 30:
            approved = _approve_pending_csrs(host, project_id, bastion_ip, password)
            if approved:
                _push("certs", f"approved {approved} certificate(s)")
            last_csr_check = _t.time()

        _t.sleep(5)

    # Phase 4: Wait for cluster operators (continue CSR approval)
    _push("operators", "waiting for cluster operators")
    last_csr_check_ops = 0
    while _t.time() < deadline:
        if _t.time() - last_csr_check_ops >= 30:
            approved = _approve_pending_csrs(host, project_id, bastion_ip, password)
            if approved:
                _push("certs", f"approved {approved} certificate(s)")
            last_csr_check_ops = _t.time()

        result = _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "oc get co --no-headers 2>/dev/null",
            timeout=15,
        )
        if result:
            items = []
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
            if total > 0:
                _push("operators", f"{available_count}/{total} available", items)
                if available_count >= total:
                    break
        else:
            _push("operators", "waiting for API server")
        _t.sleep(10)

    # Phase 5: Wait for console (continue approving CSRs — serving certs
    # often arrive late and block the console route)
    _push("console", "waiting for OpenShift console")
    last_csr_check_console = 0
    while _t.time() < deadline:
        if _t.time() - last_csr_check_console >= 30:
            approved = _approve_pending_csrs(host, project_id, bastion_ip, password)
            if approved:
                _push("certs", f"approved {approved} certificate(s)")
            last_csr_check_console = _t.time()

        result = _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            f"oc get co console --no-headers 2>/dev/null | awk '{{print $3}}' && curl -sk {console_url} -o /dev/null -w '%{{http_code}}' 2>/dev/null",
            timeout=15,
        )
        if result:
            lines = result.strip().split("\n")
            co_available = lines[0].strip() == "True" if lines else False
            http_code = lines[-1].strip() if len(lines) > 1 else ""
            if co_available and http_code == "200":
                _push("console", "console ready")
                break
            elif co_available:
                _push("console", "operator ready, waiting for route")
            else:
                _push("console", "waiting for console operator")
        _push("console", "waiting for OpenShift console")
        _t.sleep(5)

    # Final CSR sweep — don't declare ready with pending certs
    for _ in range(6):
        approved = _approve_pending_csrs(host, project_id, bastion_ip, password)
        if not approved:
            break
        _push("certs", f"approved {approved} certificate(s)")
        _t.sleep(10)

    if _is_pattern_deploy(topology):
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
        if used_recert or rhcos_count == 1:
            _push("certs", "refreshing bastion certificates")
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

    elapsed_secs = int(_t.time() - start)
    _push("ready", "cluster ready")
    try:
        from app.core.database import SessionLocal
        from app.models.project import Project

        db = SessionLocal()
        p = db.query(Project).filter_by(id=project_id).first()
        if p:
            p.ocp_status = "ready"
            p.ocp_install_elapsed = elapsed_secs
            db.commit()
        db.close()
    except Exception:
        pass
    logger.info("OCP health monitor complete for %s (%s)", project_id[:8], _elapsed())


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
            with _network_lock:
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

        if _is_ocp_topology(topology):
            project.ocp_status = "monitoring"
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


def destroy_project_sync(ctx: dict):
    """Synchronously destroy a project's VMs and networks.
    ctx contains pre-captured project data (project_id, host_id, vni_map, topology, dns_provider_id, domain).
    """
    from app.core.database import SessionLocal
    from app.models.host import Host

    project_id = ctx["project_id"]
    s = SessionLocal()
    try:
        host = s.query(Host).filter_by(id=ctx["host_id"]).first()
        if not host or not host.ip_address:
            return

        # KubeVirt native: delegate destroy to operator
        if host.host_type == "kubevirt-cluster":
            from app.models.provider import Provider
            from app.services.providers import get_provider_driver

            provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if provider:
                driver = get_provider_driver(provider)
                driver.destroy_project(provider, project_id)
                logger.info("Destroy %s: kubevirt project deleted", project_id[:8])
            return

        vni_map = ctx.get("vni_map", {})
        topo = ctx.get("topology", {})

        # Destroy containers first (before networks teardown)
        pool = _get_host_pool(host, s)
        containers = _extract_containers(topo)
        for ctr in containers:
            if ctr.get("is_pod"):
                full_pod_name = f"troshka-{project_id[:8]}-{ctr['name']}"
                volumes = _find_container_volumes(
                    ctr["node_id"], topo, project_id, pool
                )
                try:
                    job_id = start_job(
                        host,
                        "/pods/destroy",
                        {
                            "pod_name": full_pod_name,
                            "project_id": project_id,
                            "volumes": [{"mount_dir": v["mount_dir"]} for v in volumes],
                        },
                    )
                    wait_for_job(host, job_id, timeout=30)
                except TroshkadError as e:
                    logger.warning(
                        "Destroy %s: failed to destroy pod %s: %s",
                        project_id[:8],
                        full_pod_name,
                        e,
                    )
            else:
                container_name = f"troshka-{project_id[:8]}-{ctr['node_id'][:8]}"
                volumes = _find_container_volumes(
                    ctr["node_id"], topo, project_id, pool
                )
                try:
                    job_id = start_job(
                        host,
                        "/containers/destroy",
                        {
                            "container_name": container_name,
                            "project_id": project_id,
                            "volumes": [{"mount_dir": v["mount_dir"]} for v in volumes],
                        },
                    )
                    wait_for_job(host, job_id, timeout=30)
                except TroshkadError as e:
                    logger.warning(
                        "Destroy %s: failed to destroy container %s: %s",
                        project_id[:8],
                        container_name,
                        e,
                    )

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
        pool = _get_host_pool(host, s)
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
        with _network_lock:
            _teardown_networks_via_troshkad(host, project_id, vni_map)

        from app.services.placement import sync_host_capacity

        sync_host_capacity(s, host)
        s.commit()

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
        try:
            from app.models.provider import Provider
            from app.services.provider_gc_service import _get_ec2_client

            provider = (
                s.query(Provider).filter_by(id=host.provider_id).first()
                if host.provider_id
                else None
            )
            if not provider and host.provider_id:
                provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if provider and provider.security_group_id:
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

        # Clean up Route-based external access (OCP Virt only)
        try:
            from app.models.provider import Provider
            from app.services.providers import get_provider_driver

            provider = None
            if host and host.provider_id:
                provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if provider and provider.type == "ocpvirt":
                driver = get_provider_driver(provider)
                driver.delete_route_access(provider, project_id)
                logger.info(
                    "Destroy %s: cleaned up Route access resources", project_id[:8]
                )
        except Exception:
            logger.warning(
                "Destroy %s: Route cleanup failed (non-fatal)",
                project_id[:8],
                exc_info=True,
            )

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
    except Exception:
        logger.exception("Destroy %s failed", project_id[:8])
    finally:
        s.close()
