"""
Deploy service — creates VMs and networks on hosts via troshkad.

Translates canvas topology into libvirt VMs and VXLAN networks,
then sends structured commands to the troshkad agent on the host.
"""
import logging
import time as _time

from app.models.host import Host
from app.services.troshkad_client import (
    start_job, wait_for_job, check_disk_usage, poll_job, TroshkadError,
)

logger = logging.getLogger(__name__)

# In-memory deploy progress tracking: project_id -> {"step": ..., "detail": ...}
_deploy_progress: dict[str, dict] = {}


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
        })
    return vms


def _find_vm_networks(vm_node_id: str, topology: dict, vni_map: dict) -> list[dict]:
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
        if network_node_id not in vni_map:
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


def _vm_dir(project_id: str) -> str:
    return f"/var/lib/troshka/vms/{project_id}"


def _disk_path(project_id: str, vm_node_id: str, disk_node_id: str, fmt: str) -> str:
    return f"{_vm_dir(project_id)}/{vm_node_id[:8]}-{disk_node_id[:8]}.{fmt}"


def _seed_path(project_id: str, vm_node_id: str) -> str:
    return f"{_vm_dir(project_id)}/{vm_node_id[:8]}-seed.iso"


def _resolve_boot_devs(vm: dict, vm_disks: list[dict], topology: dict) -> list[str]:
    boot_type_map = {"hd": "hd", "disk": "hd", "network": "network", "cdrom": "cdrom"}
    all_nodes = topology.get("nodes", [])
    storage_nodes = {n["id"]: n for n in all_nodes if n.get("type") == "storageNode"}

    raw_boot_devs = vm.get("boot_devices") or None
    has_iso = any(d["format"] == "iso" for d in vm_disks)
    has_disk = any(d["format"] != "iso" for d in vm_disks)
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
                cache_path = f"/var/lib/troshka/images/{item_id}.{fmt}"
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
                cache_path = f"/var/lib/troshka/cache/patterns/{pattern_id}/{pattern_disk_id}.{pd.format}"
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

    # Generate presigned URLs and start download jobs
    active_jobs = []
    for ic in items_to_cache:
        url = s3_storage.generate_presigned_url(ic["s3_key"], expires=7200)
        try:
            job_id = start_job(host, "/images/cache", {
                "url": url,
                "dest_path": ic["cache_path"],
                "expected_size": ic.get("expected_size", 0),
                "expected_format": "qcow2" if ic["cache_path"].endswith(".qcow2") else None,
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
                elif job["status"] == "failed":
                    failed.add(aj["job_id"])
                    logger.error("cache: %s failed: %s", aj["name"], job.get("result", {}).get("error", ""))
            except TroshkadError:
                pass  # Transient connection error, retry next poll

        if progress_callback:
            done_count = len(completed) + len(failed)
            done_bytes = int(total_expected * done_count / max(len(active_jobs), 1))
            progress_callback(done_bytes, total_expected)

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


def _create_seed_isos_via_troshkad(host, project_id, topology):
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
        path = _seed_path(project_id, node_id)

        seeds.append({
            "path": path,
            "user_data": userdata,
            "meta_data": metadata,
        })

    if not seeds:
        return

    try:
        job_id = start_job(host, "/seeds/create-batch", {"seeds": seeds})
        job = wait_for_job(host, job_id, timeout=60)
        if job["status"] == "failed":
            logger.error("Seed ISO creation failed: %s", job.get("result", {}).get("error", ""))
    except TroshkadError as e:
        logger.error("Seed ISO creation failed: %s", e)


def _create_vm_disks_via_troshkad(host, project_id, vm, vm_disks, topology):
    """Create disk images for a VM via troshkad disks/create."""
    for disk in vm_disks:
        if disk["format"] == "iso":
            continue
        dp = _disk_path(project_id, vm["node_id"], disk["node_id"], disk["format"])

        backing = None
        if disk.get("source") == "pattern" and disk.get("patternId"):
            backing = f"/var/lib/troshka/cache/patterns/{disk['patternId']}/{disk['patternDiskId']}.{disk['format']}"
        elif disk.get("source") == "library" and disk.get("library_item_id"):
            backing = f"/var/lib/troshka/images/{disk['library_item_id']}.{disk['format']}"

        params = {
            "path": dp,
            "size_gb": disk["size_gb"],
            "format": disk["format"],
        }
        if backing:
            params["backing_file"] = backing

        try:
            job_id = start_job(host, "/disks/create", params)
            wait_for_job(host, job_id, timeout=300)
        except TroshkadError as e:
            raise RuntimeError(f"Disk creation failed for {dp}: {e}")


def _create_vm_via_troshkad(host, project_id, vm, topology, vni_map):
    """Create a VM definition via troshkad vms/create."""
    vm_name = _vm_domain_name(project_id, vm["node_id"])
    vm_disks = _find_vm_disks(vm["node_id"], topology)
    vm_networks = _find_vm_networks(vm["node_id"], topology, vni_map)

    # Build disk list for virt-install
    vm_dir = _vm_dir(project_id)
    disks = []
    for disk in vm_disks:
        if disk["format"] == "iso":
            if disk.get("library_item_id"):
                cache_path = f"/var/lib/troshka/images/{disk['library_item_id']}.iso"
                link_path = f"{vm_dir}/{vm['node_id'][:8]}-{disk['library_item_id'][:8]}.iso"
                disks.append({"path": link_path, "bus": "sata", "device": "cdrom", "symlink_from": cache_path})
            continue
        dp = _disk_path(project_id, vm["node_id"], disk["node_id"], disk["format"])
        disks.append({"path": dp, "bus": disk["bus"]})

    # Seed ISO as cdrom
    if vm.get("cloud_init"):
        disks.append({"path": _seed_path(project_id, vm["node_id"]), "bus": "sata", "device": "cdrom"})

    # Build network list
    networks = []
    for net in vm_networks:
        entry = {"bridge": net["bridge"], "model": "virtio"}
        if net["mac"]:
            entry["mac"] = net["mac"]
        networks.append(entry)

    params = {
        "domain_name": vm_name,
        "vcpus": vm["vcpus"],
        "ram_mb": vm["ram_gb"] * 1024,
        "disks": disks,
        "networks": networks,
    }

    job_id = start_job(host, "/vms/create", params)
    job = wait_for_job(host, job_id, timeout=600)
    if job["status"] == "failed":
        raise RuntimeError(f"VM creation failed for {vm_name}: {job.get('result', {}).get('error', '')}")
    return vm_name


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
    """Start VMs respecting start order via troshkad vms/start."""
    vms = _extract_vms(topology)
    start_order = topology.get("startOrder", [])

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

    # Start any VMs not in start order
    for vm in vms:
        if vm["node_id"] not in ordered_vm_ids:
            vm_name = _vm_domain_name(project_id, vm["node_id"])
            try:
                job_id = start_job(host, "/vms/start", {"domain_name": vm_name})
                wait_for_job(host, job_id, timeout=60)
            except TroshkadError as e:
                logger.warning("Failed to start VM %s: %s", vm_name, e)


def deploy_project_async(project_id: str):
    """Background thread: deploy a project's topology to a host."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "deploying":
            return

        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.ip_address:
            project.state = "error"
            project.deploy_error = "Host not available"
            s.commit()
            return

        topology = project.topology
        vni_map = project.vni_map or {}

        # Step 1: Set up VXLAN networks
        _deploy_progress[project_id] = {"step": "networking", "detail": "configuring VXLAN"}
        logger.info("Deploy %s: setting up networks on %s", project_id[:8], host.ip_address)

        net_result = _setup_networks_via_troshkad(host, topology, vni_map, s, project_id)
        if net_result is not True:
            logger.error("Deploy %s: %s", project_id[:8], net_result)
            project.state = "error"
            project.deploy_error = net_result
            s.commit()
            _deploy_progress.pop(project_id, None)
            return

        # Step 1b: Allocate and associate EIPs
        external_ips = topology.get("externalIps", [])
        if external_ips:
            _deploy_progress[project_id] = {"step": "eips", "detail": "allocating elastic IPs"}
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

            # Sync SG rules for port forwards
            gateway_node = next(
                (n for n in topology.get("nodes", [])
                 if n.get("type") == "networkNode" and n.get("data", {}).get("subtype") == "gateway"),
                None,
            )
            if gateway_node and gateway_node.get("data", {}).get("gatewayMode") == "nat-portforward":
                desired_sg = []
                for pf in gateway_node.get("data", {}).get("portForwards", []):
                    if pf.get("extPort"):
                        desired_sg.append({
                            "project_id": project_id,
                            "ext_port": int(pf["extPort"]),
                            "protocol": "tcp",
                        })
                sync_security_group_rules(s, provider, desired_sg)

        # Step 2: Create cloud-init seed ISOs
        _deploy_progress[project_id] = {"step": "cloud-init", "detail": "creating seed ISOs"}
        logger.info("Deploy %s: creating cloud-init seed ISOs", project_id[:8])
        _create_seed_isos_via_troshkad(host, project_id, topology)

        # Step 2b: Deploy metadata service
        _deploy_progress[project_id] = {"step": "cloud-init", "detail": "deploying metadata service"}
        logger.info("Deploy %s: deploying metadata service", project_id[:8])
        _setup_metadata_via_troshkad(host, project_id, topology, vni_map)

        # Step 3: Cache library images on host
        _deploy_progress[project_id] = {"step": "downloading images", "detail": "0%"}
        logger.info("Deploy %s: caching library images", project_id[:8])
        def _deploy_dl_progress(downloaded, total):
            pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
            _deploy_progress[project_id] = {"step": "downloading images", "detail": pct}
        cache_library_images(topology, host, s, progress_callback=_deploy_dl_progress)

        # Step 4: Create VM disks and definitions
        _deploy_progress[project_id] = {"step": "creating", "detail": "VMs"}
        logger.info("Deploy %s: creating VMs", project_id[:8])
        vms = _extract_vms(topology)
        for vm in vms:
            vm_disks = _find_vm_disks(vm["node_id"], topology)
            _create_vm_disks_via_troshkad(host, project_id, vm, vm_disks, topology)
            _create_vm_via_troshkad(host, project_id, vm, topology, vni_map)

        # Step 5: Start VMs
        _deploy_progress[project_id] = {"step": "starting", "detail": "VMs"}
        logger.info("Deploy %s: starting VMs", project_id[:8])
        _start_vms_via_troshkad(host, project_id, topology)

        project.state = "active"
        project.deploy_error = None
        project.deployed_topology = project.topology
        s.commit()
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
            return

        # Stop VMs via troshkad
        vms = _extract_vms(project.topology)
        for vm in vms:
            vm_name = _vm_domain_name(project_id, vm["node_id"])
            try:
                job_id = start_job(host, "/vms/stop", {"domain_name": vm_name})
                wait_for_job(host, job_id, timeout=60)
            except TroshkadError as e:
                logger.warning("Stop %s: failed to stop %s: %s", project_id[:8], vm_name, e)

        # Tear down networks via troshkad
        vni_map = project.vni_map or {}
        if vni_map:
            _teardown_networks_via_troshkad(host, project_id, vni_map)

        # Disassociate EIPs (but don't release — keep for redeploy)
        from app.models.elastic_ip import ElasticIp
        from app.services.eip_service import disassociate_eip
        project_eips = s.query(ElasticIp).filter_by(project_id=project_id, state="associated").all()
        for eip in project_eips:
            try:
                disassociate_eip(s, eip, host)
            except Exception:
                logger.warning("Failed to disassociate EIP %s on stop", eip.public_ip)

        project.state = "stopped"
        project.deploy_error = None
        s.commit()
        logger.info("Stop %s: complete", project_id[:8])

    except Exception:
        logger.exception("Stop %s failed", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Stop failed unexpectedly. Check server logs."
                s.commit()
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
            return

        topology = project.topology
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
            topology = project.topology

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

        # Recreate networks via troshkad
        if vni_map:
            net_result = _setup_networks_via_troshkad(host, topology, vni_map, s, project_id)
            if net_result is not True:
                project.state = "error"
                project.deploy_error = f"Network setup failed on restart: {net_result}"
                s.commit()
                return

        # Re-cache any missing library images (ISOs, base disks)
        cache_library_images(topology, host, s)

        # Start VMs via troshkad
        _start_vms_via_troshkad(host, project_id, topology)

        project.state = "active"
        project.deploy_error = None
        s.commit()
        logger.info("Start %s: complete", project_id[:8])

    except Exception:
        logger.exception("Start %s failed", project_id[:8])
        try:
            project = s.query(Project).filter_by(id=project_id).first()
            if project:
                project.state = "error"
                project.deploy_error = "Start failed unexpectedly. Check server logs."
                s.commit()
        except Exception:
            pass
    finally:
        s.close()


def destroy_project_sync(project_id: str):
    """Synchronously destroy a project's VMs and networks. Called before DB delete."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or not project.host_id:
            return

        host = s.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.ip_address:
            return

        vni_map = project.vni_map or {}
        topo = project.deployed_topology or project.topology

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
        vm_dir = _vm_dir(project_id)
        try:
            job_id = start_job(host, "/files/remove", {"paths": [vm_dir]})
            wait_for_job(host, job_id, timeout=30)
        except TroshkadError as e:
            logger.warning("Destroy %s: failed to remove VM dir: %s", project_id[:8], e)

        # Tear down networks via troshkad
        _teardown_networks_via_troshkad(host, project_id, vni_map)

        from app.services.placement import sync_host_capacity
        sync_host_capacity(s, host)
        s.commit()

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
