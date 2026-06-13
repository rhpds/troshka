"""
Deploy service — creates VMs and networks on hosts via troshkad.

Translates canvas topology into libvirt VMs and VXLAN networks,
then sends structured commands to the troshkad agent on the host.
"""
import logging
import threading
import time as _time

from app.models.host import Host
from app.services.troshkad_client import (
    start_job, wait_for_job, TroshkadError,
)
from app.services.ws_pubsub import notify_project

logger = logging.getLogger(__name__)

# In-memory deploy progress tracking: project_id -> {"step": ..., "detail": ...}
_deploy_progress: dict[str, dict] = {}

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
    entry = db_session.query(SharedCacheEntry).filter(
        SharedCacheEntry.storage_pool_id == pool.id,
        SharedCacheEntry.item_id == item_id,
        SharedCacheEntry.item_type == item_type,
    ).first()
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
    entry = db_session.query(SharedCacheEntry).filter(
        SharedCacheEntry.storage_pool_id == pool_id,
        SharedCacheEntry.item_id == item_id,
        SharedCacheEntry.item_type == item_type,
    ).first()
    if entry:
        entry.status = "ready"
        if size_bytes:
            entry.size_bytes = size_bytes
        db_session.commit()


def _wait_for_shared_cache(db_session, pool_id, item_id, item_type, timeout=600):
    """Wait for another download to complete. Returns True if ready."""
    import time as _t
    from app.models.storage_pool import SharedCacheEntry
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        db_session.expire_all()
        entry = db_session.query(SharedCacheEntry).filter(
            SharedCacheEntry.storage_pool_id == pool_id,
            SharedCacheEntry.item_id == item_id,
            SharedCacheEntry.item_type == item_type,
        ).first()
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
        vms.append({
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
        })
    return vms


def _find_vm_networks(vm_node_id: str, topology: dict, vni_map: dict, project_id: str = "") -> list[dict]:
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

        # Find the NIC data to get MAC address
        # Handle format: "nic-{nicId}-top" or "nic-{nicId}-bottom"
        vm_node = next((n for n in nodes if n["id"] == vm_node_id), None)
        mac = ""
        if vm_node:
            for nic in vm_node.get("data", {}).get("nics", []):
                if nic["id"] in handle:
                    mac = nic.get("mac", "")
                    break

        # BMC networks use a dedicated bridge (no VNI)
        net_node = next((n for n in nodes if n["id"] == network_node_id), None)
        if net_node and net_node.get("data", {}).get("networkType") == "bmc":
            # Use the bmc0 NIC's MAC if available, otherwise generate one
            bmc_mac = ""
            if vm_node:
                for nic in vm_node.get("data", {}).get("nics", []):
                    if nic.get("name", "").startswith("bmc"):
                        bmc_mac = nic.get("mac", "")
                        break
            if not bmc_mac:
                import random
                bmc_mac = "52:54:01:%02x:%02x:%02x" % (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            networks.append({
                "bridge": f"br-bmc-{project_id[:8]}",
                "mac": bmc_mac,
                "nic_id": handle,
            })
            continue

        if network_node_id not in vni_map:
            continue

        vni = vni_map[network_node_id]
        networks.append({
            "bridge": f"br-{vni}",
            "mac": mac,
            "nic_id": handle,
        })

    return networks


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

        storage_node = next((n for n in nodes if n["id"] == storage_node_id and n.get("type") == "storageNode"), None)
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

        disks.append({
            "node_id": storage_node_id,
            "name": sdata.get("name", "disk"),
            "size_gb": sdata.get("size", 10),
            "format": sdata.get("format", "qcow2"),
            "bus": bus,
            "source": sdata.get("source", "blank"),
            "library_item_id": sdata.get("libraryItemId"),
            "patternId": sdata.get("patternId"),
            "patternDiskId": sdata.get("patternDiskId"),
        })

    return disks


# ── Script generators ──

def _vm_domain_name(project_id: str, node_id: str) -> str:
    return f"troshka-{project_id[:8]}-{node_id[:8]}"


def _extract_bmc_config(topology: dict, project_id: str) -> dict | None:
    """Extract BMC configuration from topology if any VMs have BMC enabled."""
    bmc_network = None
    for node in topology.get("nodes", []):
        if node.get("type") == "networkNode" and node.get("data", {}).get("networkType") == "bmc":
            bmc_network = node
            break

    if not bmc_network:
        return None

    bmc_vms = []
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode" and node.get("data", {}).get("bmcEnabled"):
            bmc_ip = node["data"].get("bmcIp", "")
            if bmc_ip:
                bmc_vms.append({
                    "node_id": node["id"],
                    "domain_name": _vm_domain_name(project_id, node["id"]),
                    "bmc_ip": bmc_ip,
                })

    if not bmc_vms:
        return None

    return {
        "bmc_network": bmc_network["data"],
        "vms": bmc_vms,
    }


def _setup_bmc_via_troshkad(host, project_id: str, bmc_config: dict):
    """Start BMC endpoints (Redfish + IPMI) on the host for this project."""
    from app.services.troshkad_client import start_job, wait_for_job

    net_data = bmc_config["bmc_network"]
    cidr = net_data.get("cidr", "192.168.100.0/24")
    params = {
        "project_id": project_id,
        "bmc_cidr": cidr,
        "bmc_gateway_ip": cidr.rsplit(".", 1)[0] + ".1",
        "bmc_username": net_data.get("bmcUsername", "admin"),
        "bmc_password": net_data.get("bmcPassword", "password"),
        "vms": [{"domain_name": vm["domain_name"], "bmc_ip": vm["bmc_ip"]} for vm in bmc_config["vms"]],
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
        logger.warning("BMC teardown failed for %s: %s", project_id[:8], job.get("result"))


def _vm_dir(project_id: str, pool=None) -> str:
    if pool and pool.mode.startswith("shared"):
        return f"/var/lib/troshka/shared/vms/{project_id}"
    return f"/var/lib/troshka/vms/{project_id}"


def _disk_path(project_id: str, vm_node_id: str, disk_node_id: str, fmt: str, pool=None) -> str:
    return f"{_vm_dir(project_id, pool)}/{vm_node_id[:8]}-{disk_node_id[:8]}.{fmt}"


def _seed_path(project_id: str, vm_node_id: str, pool=None) -> str:
    return f"{_vm_dir(project_id, pool)}/{vm_node_id[:8]}-seed.iso"


def _image_cache_path(item_id: str, fmt: str, pool=None) -> str:
    if pool and pool.mode.startswith("shared"):
        return f"/var/lib/troshka/shared/images/{item_id}.{fmt}"
    return f"/var/lib/troshka/images/{item_id}.{fmt}"


def _pattern_cache_path(pattern_id: str, disk_id: str, fmt: str, pool=None) -> str:
    return f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{disk_id}.{fmt}"


def _resolve_boot_devs(vm: dict, vm_disks: list[dict], topology: dict) -> list[str]:
    boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
    all_nodes = topology.get("nodes", [])
    storage_nodes = {n["id"]: n for n in all_nodes if n.get("type") == "storageNode"}

    raw_boot_devs = vm.get("boot_devices") or None
    has_iso = any(d["format"] == "iso" for d in vm_disks)
    has_disk = any(d["format"] != "iso" for d in vm_disks)
    has_cdrom_controller = any(dc.get("bus") == "sata" and "cdrom" in dc.get("name", "")
                               for dc in vm.get("disk_controllers", []))
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
            dev = "cdrom" if storage_nodes[d].get("data", {}).get("format") == "iso" else "hd"
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
            if (cur_data.get("vcpus") != dep_data.get("vcpus") or
                cur_data.get("ram") != dep_data.get("ram") or
                cur_data.get("bootDevices") != dep_data.get("bootDevices")):
                changed_vms.append(node)

    return {
        "added_vms": added_vms,
        "removed_vms": removed_vms,
        "changed_vms": changed_vms,
        "added_networks": added_networks,
        "removed_networks": removed_networks,
        "has_changes": bool(added_vms or removed_vms or changed_vms or added_networks or removed_networks),
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
    from app.models.pattern import PatternDisk
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
            if item and item.s3_key:
                fmt = node.get("data", {}).get("format", "qcow2")
                cache_path = _image_cache_path(item_id, fmt, pool)
                items_to_cache.append({
                    "item_id": item_id,
                    "name": item.name,
                    "s3_key": item.s3_key,
                    "cache_path": cache_path,
                    "expected_size": item.size_bytes,
                })

    # Collect PXE boot ISOs from VM nodes
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        item_id = node.get("data", {}).get("pxeBootIsoId")
        if item_id:
            item = db_session.query(LibraryItem).filter_by(id=item_id).first()
            if item and item.s3_key:
                cache_path = _image_cache_path(item_id, "iso", pool)
                items_to_cache.append({
                    "item_id": item_id,
                    "name": item.name,
                    "s3_key": item.s3_key,
                    "cache_path": cache_path,
                    "expected_size": item.size_bytes,
                })

    # Collect pattern disks
    for node in nodes:
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        pattern_id = data.get("patternId")
        pattern_disk_id = data.get("patternDiskId")
        if pattern_id and pattern_disk_id:
            pd = db_session.query(PatternDisk).filter_by(id=pattern_disk_id, pattern_id=pattern_id).first()
            if pd and pd.s3_key:
                cache_path = _pattern_cache_path(pattern_id, pd.source_disk_id, pd.format, pool)
                items_to_cache.append({
                    "item_id": pattern_disk_id,
                    "name": f"pattern-{pattern_id[:8]}-disk-{pattern_disk_id[:8]}",
                    "s3_key": pd.s3_key,
                    "cache_path": cache_path,
                    "expected_size": pd.size_bytes,
                })

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
    if pool and pool.mode.startswith("shared"):
        items_needing_download = []
        for ic in items_to_cache:
            status, entry = _check_shared_cache(db_session, pool, ic["item_id"], "image")
            if status == "ready":
                logger.info("  %s already on shared storage, skipping", ic["name"])
                continue
            elif status == "downloading":
                logger.info("  %s being downloaded by another host, waiting...", ic["name"])
                if _wait_for_shared_cache(db_session, pool.id, ic["item_id"], "image"):
                    logger.info("  %s now available on shared storage", ic["name"])
                    continue
                else:
                    logger.warning("  %s download timed out, will retry", ic["name"])
            # Need to download — create/update cache entry
            rel_path = ic["cache_path"].replace("/var/lib/troshka/shared/", "")
            _create_shared_cache_entry(db_session, pool, ic["item_id"], "image", rel_path)
            items_needing_download.append(ic)
        items_to_cache = items_needing_download

    # Start download jobs using aws s3 cp
    from app.services.s3_storage import _get_s3_config
    s3_creds = _get_s3_config()
    s3_bucket = s3_storage._bucket()
    active_jobs = []
    for ic in items_to_cache:
        s3_url = f"s3://{s3_bucket}/{ic['s3_key']}"
        try:
            job_id = start_job(host, "/images/cache", {
                "s3_url": s3_url,
                "dest_path": ic["cache_path"],
                "expected_size": ic.get("expected_size", 0),
                "expected_format": "qcow2" if ic["cache_path"].endswith(".qcow2") else None,
                "aws_access_key_id": s3_creds.get("access_key_id", ""),
                "aws_secret_access_key": s3_creds.get("secret_access_key", ""),
                "aws_region": s3_creds.get("region", "us-east-1"),
            })
            active_jobs.append({"job_id": job_id, "name": ic["name"], "item_id": ic["item_id"]})
            logger.info("  cache job started: %s (%s) -> %s", ic["name"], ic["item_id"][:8], ic["cache_path"])
        except TroshkadError as e:
            logger.error("Failed to start cache job for %s: %s", ic["name"], e)

    if not active_jobs:
        return

    # Poll until all jobs complete
    total_expected = sum(ic["expected_size"] for ic in items_to_cache)
    completed = set()
    failed = set()
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
                        _mark_shared_cache_ready(db_session, pool.id, aj["item_id"], "image")
                elif job["status"] == "failed":
                    failed.add(aj["job_id"])
                    logger.error("cache: %s failed: %s", aj["name"], job.get("result", {}).get("error", ""))
            except TroshkadError:
                pass  # Transient connection error, retry next poll

        if progress_callback:
            done_count = len(completed) + len(failed)
            # Try to get byte-level progress from running jobs
            in_progress = []
            for aj in active_jobs:
                if aj["job_id"] not in completed and aj["job_id"] not in failed:
                    try:
                        job = poll_job(host, aj["job_id"])
                        output = job.get("output", [])
                        for line in reversed(output):
                            if "%" in line and ("/" in line or "MiB" in line or "GiB" in line):
                                in_progress.append(line.strip().split("\r")[-1].strip())
                                break
                    except TroshkadError:
                        pass
            detail = f"{done_count}/{len(active_jobs)}"
            if in_progress:
                line = in_progress[0]
                if "AWSAccessKey" in line or "Signature" in line or "https://" in line:
                    line = ""
                if line:
                    detail += f" | {line}"
            progress_callback(detail, None)

        if len(completed) + len(failed) == last_completed_count:
            stale_polls += 1
        else:
            stale_polls = 0
            last_completed_count = len(completed) + len(failed)

        if stale_polls >= 720:  # 1 hour with no progress
            logger.error("Download stalled for 1 hour, aborting")
            return

    if failed:
        logger.error("cache_library_images: %d/%d downloads failed", len(failed), len(active_jobs))


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
                pf_list.append({
                    "extPort": fe["bindPort"],
                    "intIp": gw.get("transit_ns_ip", ""),
                    "intPort": fe["bindPort"],
                    "_private_ip": lb_eip_priv,
                })
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
        job_id = start_job(host, "/networks/full-teardown", {
            "project_id": project_id,
            "vni_list": vni_list,
        })
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
            job_id = start_job(host, "/pxe/setup", {
                "project_id": project_id,
                "vni": net["vni"],
                "iso_path": iso_path,
                "gateway_ip": gateway_ip,
                "http_port": pxe.get("http_port", 8080),
                "tftp_root": pxe.get("tftp_root", ""),
            })
            job = wait_for_job(host, job_id, timeout=120)
            if job["status"] == "failed":
                logger.error("PXE setup failed for VNI %s: %s",
                             net["vni"], job.get("result", {}).get("error", ""))
        except TroshkadError as e:
            logger.error("PXE setup failed for VNI %s: %s", net["vni"], e)


def _create_seed_isos_via_troshkad(host, project_id, topology, pool=None):
    """Create cloud-init seed ISOs via troshkad seeds/create-batch."""
    from app.services.cloud_init import generate_userdata, generate_metadata

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
            logger.error("Seed ISO creation failed: %s", job.get("result", {}).get("error", ""))
    except TroshkadError as e:
        logger.error("Seed ISO creation failed: %s", e)


def _create_vm_disks_via_troshkad(host, project_id, vm, vm_disks, pool=None):
    """Create disk images for a VM via troshkad disks/create. Returns list of job IDs."""
    job_ids = []
    for disk in vm_disks:
        if disk["format"] == "iso":
            continue
        dp = _disk_path(project_id, vm["node_id"], disk["node_id"], disk["format"], pool)

        backing = None
        if disk.get("source") == "pattern" and disk.get("patternId") and disk.get("patternDiskId"):
            from app.core.database import SessionLocal as _SL
            from app.models.pattern import PatternDisk as _PD
            _s = _SL()
            _pd = _s.query(_PD).filter_by(id=disk["patternDiskId"]).first()
            _cache_disk_id = _pd.source_disk_id if _pd else disk["patternDiskId"]
            _s.close()
            backing = _pattern_cache_path(disk["patternId"], _cache_disk_id, disk["format"], pool)
        elif disk.get("source") == "library" and disk.get("library_item_id"):
            backing = _image_cache_path(disk["library_item_id"], disk["format"], pool)

        params = {
            "path": dp,
            "size_gb": disk["size_gb"],
            "format": disk["format"],
        }
        if backing:
            params["backing_file"] = backing

        job_id = start_job(host, "/disks/create", params)
        job_ids.append(job_id)
    return job_ids


def _create_vm_via_troshkad(host, project_id, vm, topology, vni_map, pool=None, disk_cache=None):
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
                link_path = f"{vm_dir}/{vm['node_id'][:8]}-{disk['library_item_id'][:8]}.iso"
                disks.append({"path": link_path, "bus": "sata", "device": "cdrom", "symlink_from": cache_path})
            continue
        dp = _disk_path(project_id, vm["node_id"], disk["node_id"], disk["format"], pool)
        disks.append({"path": dp, "bus": disk["bus"]})

    # Seed ISO as cdrom
    if vm.get("cloud_init"):
        disks.append({"path": _seed_path(project_id, vm["node_id"], pool), "bus": "sata", "device": "cdrom"})

    # Build network list
    networks = []
    for net in vm_networks:
        entry = {"bridge": net["bridge"], "model": "virtio"}
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

    job_id = start_job(host, "/vms/create", params)
    return job_id


def _setup_metadata_via_troshkad(host, project_id, topology, vni_map):
    """Deploy the cloud-init metadata service via troshkad metadata/deploy."""
    from app.services.cloud_init import generate_userdata, generate_metadata

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
                vm_configs[mac] = {"vm_name": vm_label, "userdata": userdata, "metadata": metadata}

    if not vm_configs:
        return

    bridges = [f"br-{vni}" for vni in vni_map.values()]
    ns = f"troshka-{project_id[:8]}"

    try:
        job_id = start_job(host, "/metadata/deploy", {
            "project_id": project_id,
            "bridges": bridges,
            "vm_configs": vm_configs,
            "namespace": ns,
        })
        wait_for_job(host, job_id, timeout=30)
        logger.info("Metadata service deployed for %s", project_id[:8])
    except TroshkadError as e:
        logger.warning("Metadata service deployment failed for %s: %s", project_id[:8], e)


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
                    logger.info("Deploy %s: skipping %s (auto-start disabled)", project_id[:8], vm["name"])
                    continue
                delay = entry.get("delaySeconds", 0)
                if delay > 0:
                    _time.sleep(delay)
                vm_name = _vm_domain_name(project_id, vm["node_id"])
                try:
                    job_id = start_job(host, "/vms/start", {"domain_name": vm_name})
                    wait_for_job(host, job_id, timeout=60)
                except TroshkadError as e:
                    logger.warning("Failed to start VM %s: %s", vm_name, e)
                    failed.append((vm["name"], str(e)))

    # Start any VMs not in start order (parallel)
    unordered_jobs = []
    for vm in vms:
        if vm["node_id"] not in ordered_vm_ids:
            vm_name = _vm_domain_name(project_id, vm["node_id"])
            try:
                job_id = start_job(host, "/vms/start", {"domain_name": vm_name})
                unordered_jobs.append((vm["name"], vm_name, job_id))
            except TroshkadError as e:
                logger.warning("Failed to start VM %s: %s", vm_name, e)
                failed.append((vm["name"], str(e)))
    for name, vm_name, job_id in unordered_jobs:
        try:
            wait_for_job(host, job_id, timeout=60)
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


def deploy_project_async(project_id: str, auto_start: bool = True):
    """Background thread: deploy a project's topology to a host."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "deploying":
            return

        host = s.query(Host).filter_by(id=project.host_id).first() if project.host_id else None
        if not host:
            from app.services.placement import find_available_host, calculate_project_requirements
            reqs = calculate_project_requirements(project.topology or {})
            host = find_available_host(s, reqs["total_vcpus"], reqs["total_ram_mb"])
            if host:
                project.host_id = host.id
                s.commit()
                logger.info("Deploy %s: auto-placed on host %s", project_id[:8], host.id[:8])
        if not host or not host.ip_address:
            project.state = "error"
            project.deploy_error = "Host not available"
            s.commit()
            notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": "Host not available"})
            return

        topology = project.topology or {}
        vni_map = project.vni_map or {}
        if not vni_map:
            from app.services.vxlan import allocate_vnis_for_project
            vni_map = allocate_vnis_for_project(s, topology)
            project.vni_map = vni_map
            s.commit()
            logger.info("Deploy %s: allocated VNIs %s", project_id[:8], vni_map)

        pool = _get_host_pool(host, s)
        disk_cache = "none" if pool and pool.mode.startswith("shared") else None

        # Step 0: Allocate and associate EIPs (before networking so DNAT rules have private IPs)
        external_ips = topology.get("externalIps", [])
        if external_ips:
            _deploy_progress[project_id] = {"step": "eips", "detail": "allocating elastic IPs"}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
            logger.info("Deploy %s: allocating %d EIPs", project_id[:8], len(external_ips))
            from app.services.eip_service import allocate_eip, associate_eip, sync_security_group_rules
            from app.models.elastic_ip import ElasticIp
            from app.models.provider import Provider

            provider = s.query(Provider).filter_by(id=project.provider_id).first() if project.provider_id else None
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
                existing = s.query(ElasticIp).filter_by(
                    project_id=project_id, canvas_eip_id=canvas_id
                ).first()
                if existing:
                    eip = existing
                else:
                    eip = allocate_eip(s, provider, project_id, canvas_id)

                if eip.state != "associated":
                    associate_eip(s, eip, host)

                ext_ip["ip"] = eip.public_ip
                ext_ip["_private_ip"] = eip.private_ip

            project.topology = topology
            s.commit()

        # Step 1: Set up VXLAN networks (serialized to avoid nftables contention)
        _deploy_progress[project_id] = {"step": "networking", "detail": "waiting for lock"}
        notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
        with _network_lock:
            _deploy_progress[project_id] = {"step": "networking", "detail": "configuring VXLAN"}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
            logger.info("Deploy %s: setting up networks on %s", project_id[:8], host.ip_address)

            net_result = _setup_networks_via_troshkad(host, topology, vni_map, s, project_id)
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
            _deploy_progress[project_id] = {"step": "load balancer", "detail": "starting HAProxy"}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
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
            from app.services.eip_service import sync_security_group_rules
            from app.models.provider import Provider as _Prov
            _provider = s.query(_Prov).filter_by(id=project.provider_id).first() if project.provider_id else None
            if not _provider and host.provider_id:
                _provider = s.query(_Prov).filter_by(id=host.provider_id).first()
            if _provider:
                desired_sg = []
                gateway_node = next(
                    (n for n in topology.get("nodes", [])
                     if n.get("type") == "networkNode" and n.get("data", {}).get("subtype") == "gateway"),
                    None,
                )
                if gateway_node and gateway_node.get("data", {}).get("gatewayMode") == "nat-portforward":
                    for pf in gateway_node.get("data", {}).get("portForwards", []):
                        if pf.get("extPort"):
                            desired_sg.append({
                                "project_id": project_id,
                                "ext_port": int(pf["extPort"]),
                                "protocol": "tcp",
                            })
                if lb_config and lb_config.get("frontends") and lb_config.get("external", True):
                    for fe in lb_config["frontends"]:
                        desired_sg.append({
                            "project_id": project_id,
                            "ext_port": int(fe["bindPort"]),
                            "protocol": "tcp",
                        })
                if desired_sg:
                    sync_security_group_rules(s, _provider, desired_sg)

        if _project_deleted(project_id):
            logger.info("Deploy %s: project deleted mid-deploy, aborting", project_id[:8])
            _deploy_progress.pop(project_id, None)
            return

        # Step 2: Create cloud-init seed ISOs
        _deploy_progress[project_id] = {"step": "cloud-init", "detail": "creating seed ISOs"}
        notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
        logger.info("Deploy %s: creating cloud-init seed ISOs", project_id[:8])
        _create_seed_isos_via_troshkad(host, project_id, topology, pool)

        # Step 2b: Deploy metadata service
        _deploy_progress[project_id] = {"step": "cloud-init", "detail": "deploying metadata service"}
        notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
        logger.info("Deploy %s: deploying metadata service", project_id[:8])
        _setup_metadata_via_troshkad(host, project_id, topology, vni_map)

        if _project_deleted(project_id):
            logger.info("Deploy %s: project deleted mid-deploy, aborting", project_id[:8])
            _deploy_progress.pop(project_id, None)
            return

        # Step 3: Cache library images on host
        _deploy_progress[project_id] = {"step": "downloading images", "detail": "0%"}
        notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
        logger.info("Deploy %s: caching library images", project_id[:8])
        def _deploy_dl_progress(detail, _total):
            _deploy_progress[project_id] = {"step": "downloading images", "detail": str(detail)}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
        cache_library_images(topology, host, s, progress_callback=_deploy_dl_progress)

        # Step 3b: Set up PXE boot services (extract kernel/initrd, start HTTP server)
        logger.info("Deploy %s: setting up PXE boot services", project_id[:8])
        _setup_pxe_via_troshkad(host, topology, vni_map, project_id)

        # Step 3c: Validate BMC configuration
        bmc_network_exists = any(
            n.get("type") == "networkNode" and n.get("data", {}).get("networkType") == "bmc"
            for n in topology.get("nodes", [])
        )
        if bmc_network_exists:
            missing_bmc_ips = [
                n["data"].get("name", n["id"][:8])
                for n in topology.get("nodes", [])
                if n.get("type") == "vmNode" and n.get("data", {}).get("bmcEnabled") and not n.get("data", {}).get("bmcIp")
            ]
            if missing_bmc_ips:
                error_msg = f"BMC-enabled VMs missing BMC IP: {', '.join(missing_bmc_ips)}"
                logger.error("Deploy %s: %s", project_id[:8], error_msg)
                project.state = "error"
                project.deploy_error = error_msg
                s.commit()
                notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": error_msg})
                _deploy_progress.pop(project_id, None)
                return

        # Create BMC bridge (before VMs so libvirt can validate the bridge name)
        bmc_config = _extract_bmc_config(topology, project_id)
        if bmc_config:
            from app.services.troshkad_client import start_job as _sj, wait_for_job as _wj
            net_data = bmc_config["bmc_network"]
            cidr = net_data.get("cidr", "192.168.100.0/24")
            _bj = _sj(host, "/bmc/create-bridge", {
                "project_id": project_id,
                "bmc_cidr": cidr,
                "bmc_gateway_ip": cidr.rsplit(".", 1)[0] + ".1",
                "vms": [{"bmc_ip": vm["bmc_ip"]} for vm in bmc_config["vms"]],
            })
            _wj(host, _bj, timeout=30)
            logger.info("Deploy %s: BMC bridge created", project_id[:8])

        if _project_deleted(project_id):
            logger.info("Deploy %s: project deleted mid-deploy, aborting", project_id[:8])
            _deploy_progress.pop(project_id, None)
            return

        # Step 4: Create VM disks and definitions (parallel)
        _deploy_progress[project_id] = {"step": "creating", "detail": "VMs"}
        notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
        logger.info("Deploy %s: creating VMs", project_id[:8])
        vms = _extract_vms(topology)

        # Fire all disk creation jobs in parallel
        disk_jobs = []
        for vm in vms:
            vm_disks = _find_vm_disks(vm["node_id"], topology)
            job_ids = _create_vm_disks_via_troshkad(host, project_id, vm, vm_disks, pool)
            disk_jobs.extend(job_ids if isinstance(job_ids, list) else [])
        for jid in disk_jobs:
            try:
                wait_for_job(host, jid, timeout=300)
            except TroshkadError as e:
                logger.error("Deploy %s: disk creation failed: %s", project_id[:8], e)

        # Create VM definitions sequentially (virt-install storage pool race condition)
        for vm in vms:
            job_id = _create_vm_via_troshkad(host, project_id, vm, topology, vni_map, pool, disk_cache)
            if job_id:
                try:
                    wait_for_job(host, job_id, timeout=300)
                except TroshkadError as e:
                    logger.error("Deploy %s: VM creation failed: %s", project_id[:8], e)

        # Step 4b: Start BMC endpoints (after VMs are defined, before startup)
        bmc_config = _extract_bmc_config(topology, project_id)
        if bmc_config:
            _deploy_progress[project_id] = {"step": "bmc", "detail": "starting BMC endpoints"}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
            logger.info("Deploy %s: starting BMC endpoints for %d VMs", project_id[:8], len(bmc_config["vms"]))
            bmc_result = _setup_bmc_via_troshkad(host, project_id, bmc_config)
            if bmc_result is not True:
                logger.error("Deploy %s: BMC setup failed: %s", project_id[:8], bmc_result)
                project.state = "error"
                project.deploy_error = f"BMC setup failed: {bmc_result}"
                s.commit()
                _deploy_progress.pop(project_id, None)
                return

        # Step 5: Start VMs (unless auto_start is disabled)
        if auto_start:
            _deploy_progress[project_id] = {"step": "starting", "detail": "VMs"}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
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
                notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": error_msg})
                _deploy_progress.pop(project_id, None)
                return

        project.state = "active" if auto_start else "stopped"
        project.deploy_error = None
        project.deployed_topology = project.topology

        # Create DNS records if DNS provider configured
        if project.dns_provider_id and project.guid and project.domain:
            from app.models.dns_provider import DnsProvider
            from app.services.dns_service import resolve_dns_records, create_dns_records

            dns_provider = s.query(DnsProvider).filter_by(id=project.dns_provider_id).first()
            if dns_provider and lb_config:
                _deploy_progress[project_id] = {"step": "dns", "detail": f"creating records for {project.guid}.{project.domain}"}
                notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})

                eip_address = None
                for ext_ip in external_ips:
                    pub = ext_ip.get("ip") or ext_ip.get("_public_ip")
                    if pub:
                        eip_address = pub
                        break

                dns_templates = lb_config.get("dns_records", [])
                if dns_templates:
                    records = resolve_dns_records(dns_templates, guid=project.guid, domain=project.domain, eip=eip_address)
                    errors = create_dns_records(dns_provider.type, dns_provider.config, records, ttl=lb_config.get("dns_ttl", 30))

                    deployed_topo = project.deployed_topology or {}
                    deployed_topo["_dns_records"] = [r for r in records if r.get("value")]
                    project.deployed_topology = deployed_topo

                    if errors:
                        logger.warning("Deploy %s: DNS record creation had errors: %s", project_id[:8], errors)

        # Store BMC addresses in deployed topology for UI display
        if bmc_config:
            deployed_topo = project.deployed_topology or {}
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
            project.deployed_topology = deployed_topo

        s.commit()
        notify_project(project_id, {"type": "project-state", "state": "active", "deploy_error": None})
        vm_states = {vm["node_id"]: "running" for vm in vms}
        notify_project(project_id, {"type": "vm-state", "states": vm_states, "progress": {}})
        _deploy_progress.pop(project_id, None)
        logger.info("Deploy %s: complete — all VMs running", project_id[:8])

    except Exception:
        logger.exception("Deploy %s failed unexpectedly", project_id[:8])
        _deploy_progress.pop(project_id, None)
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Unexpected deploy error. Check server logs."
                s.commit()
                notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": project.deploy_error})
        except Exception:
            pass
    finally:
        s.close()


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
        if not host or not host.ip_address:
            project.state = "error"
            project.deploy_error = "Host not available"
            s.commit()
            notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": "Host not available"})
            return

        # Stop VMs via troshkad
        topology = project.topology or {}
        vms = _extract_vms(topology)
        for vm in vms:
            vm_name = _vm_domain_name(project_id, vm["node_id"])
            try:
                job_id = start_job(host, "/vms/stop", {"domain_name": vm_name})
                wait_for_job(host, job_id, timeout=90)
            except TroshkadError as e:
                logger.warning("Stop %s: failed to stop %s: %s", project_id[:8], vm_name, e)

        # BMC, networks, and EIPs stay intact on stop — only torn down on delete
        project.state = "stopped"
        project.deploy_error = None
        s.commit()
        notify_project(project_id, {"type": "project-state", "state": "stopped", "deploy_error": None})
        logger.info("Stop %s: complete", project_id[:8])

    except Exception:
        logger.exception("Stop %s failed", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Stop failed unexpectedly. Check server logs."
                s.commit()
                notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": project.deploy_error})
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
        if not host or not host.ip_address:
            project.state = "error"
            project.deploy_error = "Host not available"
            s.commit()
            notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": "Host not available"})
            return

        topology = project.topology or {}
        vni_map = project.vni_map or {}

        # Re-associate EIPs first so topology has _private_ip for DNAT rules
        from app.models.elastic_ip import ElasticIp
        from app.services.eip_service import associate_eip
        project_eips = s.query(ElasticIp).filter_by(project_id=project_id, state="allocated").all()
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
            s.execute(text("UPDATE projects SET topology = :topo WHERE id = :pid"),
                      {"topo": json.dumps(topology), "pid": project_id})
            s.commit()
            s.refresh(project)
            topology = project.topology or {}

            from app.models.provider import Provider
            from app.services.eip_service import sync_security_group_rules
            provider = s.query(Provider).filter_by(id=project.provider_id).first() if project.provider_id else None
            if not provider and host.provider_id:
                provider = s.query(Provider).filter_by(id=host.provider_id).first()
            if provider:
                gw_node = next(
                    (n for n in (topology or {}).get("nodes", [])
                     if n.get("type") == "networkNode" and n.get("data", {}).get("subtype") == "gateway"
                     and n.get("data", {}).get("gatewayMode") == "nat-portforward"),
                    None,
                )
                if gw_node:
                    desired_sg = [
                        {"project_id": project_id, "ext_port": int(pf["extPort"]), "protocol": "tcp"}
                        for pf in gw_node.get("data", {}).get("portForwards", [])
                        if pf.get("extPort")
                    ]
                    sync_security_group_rules(s, provider, desired_sg)

        # Recreate networks via troshkad (serialized to avoid nftables contention)
        if vni_map:
            with _network_lock:
                net_result = _setup_networks_via_troshkad(host, topology, vni_map, s, project_id)
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
            notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": error_msg})
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
        s.commit()
        notify_project(project_id, {"type": "project-state", "state": "active", "deploy_error": None})
        logger.info("Start %s: complete", project_id[:8])

    except Exception:
        logger.exception("Start %s failed", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Start failed unexpectedly. Check server logs."
                s.commit()
                notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": project.deploy_error})
        except Exception:
            pass
    finally:
        s.close()


def destroy_project_sync(ctx: dict):
    """Synchronously destroy a project's VMs and networks.
    ctx contains pre-captured project data (project_id, host_id, vni_map, topology, dns_provider_id, domain)."""
    from app.core.database import SessionLocal
    from app.models.host import Host

    project_id = ctx["project_id"]
    s = SessionLocal()
    try:
        host = s.query(Host).filter_by(id=ctx["host_id"]).first()
        if not host or not host.ip_address:
            return

        vni_map = ctx.get("vni_map", {})
        topo = ctx.get("topology", {})

        # Destroy VMs via troshkad
        vms = _extract_vms(topo)
        for vm in vms:
            vm_name = _vm_domain_name(project_id, vm["node_id"])
            try:
                job_id = start_job(host, "/vms/destroy", {"domain_name": vm_name})
                wait_for_job(host, job_id, timeout=60)
            except TroshkadError as e:
                logger.warning("Destroy %s: failed to destroy %s: %s", project_id[:8], vm_name, e)

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

        # Tear down BMC endpoints (sushy-emulator, vbmcd)
        try:
            _teardown_bmc_via_troshkad(host, project_id)
        except Exception as e:
            logger.warning("Destroy %s: BMC teardown failed (non-fatal): %s", project_id[:8], e)

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

            dns_provider = s.query(DnsProvider).filter_by(id=ctx["dns_provider_id"]).first()
            dns_records = topo.get("_dns_records", [])
            if dns_provider and dns_records:
                logger.info("Teardown %s: deleting DNS records", project_id[:8])
                delete_dns_records(dns_provider.type, dns_provider.config, dns_records)

        # Clean up security group rules for this project
        try:
            from app.models.provider import Provider
            from app.services.eip_service import _get_ec2_client
            provider = s.query(Provider).filter_by(id=host.provider_id).first() if host.provider_id else None
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
                                    IpPermissions=[{
                                        "IpProtocol": perm["IpProtocol"],
                                        "FromPort": perm["FromPort"],
                                        "ToPort": perm["ToPort"],
                                        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": desc}],
                                    }],
                                )
                            except Exception:
                                pass
        except Exception as e:
            logger.warning("Destroy %s: SG cleanup failed (non-fatal): %s", project_id[:8], e)

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
