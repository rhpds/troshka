"""
Deploy service — creates VMs and networks on hosts via troshkad.

Translates canvas topology into libvirt VMs and VXLAN networks,
then sends structured commands to the troshkad agent on the host.
"""

import copy
import datetime
import logging
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
from app.services.deploy_topology import (
    _auto_assign_container_ips,
    _disk_path,
    _extract_bmc_config,
    _extract_containers,
    _extract_vms,
    _filter_topology_for_host,
    _find_container_networks,
    _find_container_volumes,
    _find_vm_disks,
    _find_vm_name_by_ip,
    _find_vm_networks,
    _image_cache_path,
    _pattern_cache_path,
    _seed_path,
    _snapshot_cache_path,
    _vm_dir,
    _vm_domain_name,
)
from app.services.mesh_service import (
    create_mesh_peers,
    delete_mesh_peers,
    get_peer_config_for_host,
)
from app.services.troshkad_client import (
    TroshkadError,
    start_job,
    troshkad_request,
    wait_for_job,
)
from app.services.ws_pubsub import notify_project

logger = logging.getLogger(__name__)

_deploy_semaphore = RedisSemaphore("deploy", limit=100, ttl=7200)


class DeployError(Exception):
    """Raised when a deploy cannot proceed (e.g. pattern storage not ready)."""


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
_VMS_DESTROY_PATH = "/vms/destroy"
_MSG_BROWSER_CREDS = "browser credentials"
_KILL_BROWSER_CMD = (
    "pkill -x firefox 2>/dev/null || true; "
    "pkill -f '[f]irefox' 2>/dev/null || true; "
    "sleep 2; "
    "rm -f /home/cloud-user/.mozilla/firefox/*.default*/lock "
    "/home/cloud-user/.mozilla/firefox/*.default*/.parentlock 2>/dev/null || true"
)
_ENSURE_FIREFOX_PROFILE_CMD = (
    "if ! find /home/cloud-user/.mozilla/firefox -maxdepth 2 "
    "-name cert9.db 2>/dev/null | grep -q .; then "
    "firefox --headless --no-remote >/dev/null 2>&1 & "
    "FXPID=$!; sleep 5; "
    "kill $FXPID 2>/dev/null; wait $FXPID 2>/dev/null || true; "
    "sleep 2; fi"
)
_CLEAR_BASTION_OCP_COOKIES_CMD = (
    'python3 -c "import glob, sqlite3; '
    "[(lambda p: (c := sqlite3.connect(p), c.execute("
    "'DELETE FROM moz_cookies WHERE host LIKE ?', ('%.ocp.ocp.local',)), "
    "c.commit(), c.close()))(p) "
    "for p in glob.glob('/home/cloud-user/.mozilla/firefox/*/cookies.sqlite')]\" "
    "2>/dev/null || true"
)
_LOG_DEPLOY = "Deploy %s: %s"
_KUBEVIRT_API = "kubevirt.io"
_VM_START_FAILED = "Failed to start VM %s: %s"


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


def _ensure_storage_library_ref(node, db_session):
    """Ensure a storage node has source=library and a resolved libraryItemId."""
    data = node.get("data", {})
    if data.get("source") == "pattern" or data.get("patternDiskId"):
        return
    if not data.get("libraryItemName") and not data.get("libraryItemId"):
        return
    if data.get("libraryItemName") or data.get("libraryItemId"):
        data.setdefault("source", "library")
    item_id = data.get("libraryItemId")
    if item_id:
        return
    _resolve_library_item_by_name(node, item_id, db_session)


def _prepare_topology_library_refs(topology, db_session, project=None):
    """Resolve libraryItemName refs on storage nodes before image cache / disk create."""
    changed = False
    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        before = (
            node.get("data", {}).get("libraryItemId"),
            node.get("data", {}).get("source"),
        )
        _ensure_storage_library_ref(node, db_session)
        after = (
            node.get("data", {}).get("libraryItemId"),
            node.get("data", {}).get("source"),
        )
        if after != before:
            changed = True
    if changed and project is not None:
        project.topology = topology
        db_session.commit()


def _collect_library_items(nodes, db_session, pool):
    """Collect library items from storage nodes for caching."""
    from app.models.library import LibraryItem

    items = []
    for node in nodes:
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        if data.get("source") == "pattern" or data.get("patternDiskId"):
            continue
        _ensure_storage_library_ref(node, db_session)
        item_id = node.get("data", {}).get("libraryItemId")
        if not item_id:
            continue
        item = db_session.query(LibraryItem).filter_by(id=item_id).first()
        if not item:
            item, item_id = _resolve_library_item_by_name(
                node,
                item_id,
                db_session,
            )
        if not item or not item.s3_key:
            continue
        fmt = node.get("data", {}).get("format", "qcow2")
        cache_path = _image_cache_path(item_id, fmt, pool)
        items.append(
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
    return items


def _resolve_library_item_by_name(node, item_id, db_session):
    """Try to resolve a library item by name when ID lookup fails."""
    from app.models.library import LibraryItem

    item_name = node.get("data", {}).get("libraryItemName")
    fmt = node.get("data", {}).get("format", "qcow2")
    if not item_name:
        return None, item_id
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
            item_id[:8] if item_id else "?",
            item_name,
            item.id[:8],
        )
        node["data"]["libraryItemId"] = item.id
        item_id = item.id
    return item, item_id


def _collect_pxe_boot_isos(nodes, db_session, pool):
    """Collect PXE boot ISO items from VM nodes for caching."""
    from app.models.library import LibraryItem

    items = []
    for node in nodes:
        if node.get("type") != "vmNode":
            continue
        item_id = node.get("data", {}).get("pxeBootIsoId")
        if not item_id:
            continue
        item = db_session.query(LibraryItem).filter_by(id=item_id).first()
        if not item or not item.s3_key:
            continue
        cache_path = _image_cache_path(item_id, "iso", pool)
        items.append(
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
    return items


def _collect_pattern_disks(nodes, db_session, pool, provider_id=None):
    """Collect pattern disk items from storage nodes for caching."""
    from app.models.pattern import Pattern, PatternDisk
    from app.services.pattern_locations import pattern_disk_source_for_cluster
    from app.services.s3_storage import (
        _get_s3_config,
        cluster_s3_to_upload_creds,
        get_cluster_s3_config,
    )

    items = []
    for node in nodes:
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        pattern_id = data.get("patternId")
        pattern_disk_id = data.get("patternDiskId")
        if not (pattern_id and pattern_disk_id):
            continue
        pd = (
            db_session.query(PatternDisk)
            .filter_by(id=pattern_disk_id, pattern_id=pattern_id)
            .first()
        )
        if not pd or not pd.s3_key:
            continue
        cache_path = _pattern_cache_path(
            pattern_id,
            pd.source_disk_id,
            pd.format,
            pool,
        )
        disk_name = data.get("label") or data.get("name") or node.get("id", "")[:8]
        pattern_obj = db_session.query(Pattern).filter_by(id=pattern_id).first()
        source = pattern_disk_source_for_cluster(
            db_session, pattern_disk_id, provider_id
        )
        source_provider_id = (
            pattern_obj.source_provider_id if source == "obc" and pattern_obj else None
        )
        item = {
            "item_id": pattern_disk_id,
            "name": disk_name,
            "s3_key": pd.s3_key,
            "cache_path": cache_path,
            "expected_size": pd.size_bytes,
            "source": source or "local",
            "source_provider_id": source_provider_id,
        }
        if source == "obc" and source_provider_id:
            obc_cfg = get_cluster_s3_config(db_session, source_provider_id)
            if obc_cfg:
                item["download_creds"] = cluster_s3_to_upload_creds(obc_cfg)
        elif source == "central":
            item["download_creds"] = _get_s3_config()
        items.append(item)
    return items


def _snapshot_disk_to_cache_item(sd, snapshot_item_id, data):
    """Convert a snapshot disk record to a cache item dict, or None if no s3_key."""
    if not sd.s3_key:
        return None
    parts = sd.s3_key.rsplit("/", 1)[-1].rsplit(".", 1)
    orig_disk_id = parts[0] if parts else sd.id
    cache_path = _snapshot_cache_path(
        snapshot_item_id,
        orig_disk_id,
        sd.format,
    )
    label = data.get("label") or data.get("name") or snapshot_item_id[:8]
    return {
        "item_id": sd.id,
        "name": label,
        "s3_key": sd.s3_key,
        "cache_path": cache_path,
        "expected_size": sd.size_bytes,
        "source": "local",
        "source_provider_id": None,
    }


def _collect_snapshot_disks(nodes, db_session):
    """Collect snapshot disk items from storage nodes for caching."""
    from app.models.library import LibraryItemDisk

    items = []
    for node in nodes:
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        if data.get("source") != "snapshot":
            continue
        snapshot_item_id = data.get("snapshotItemId")
        if not snapshot_item_id:
            continue
        snap_disks = (
            db_session.query(LibraryItemDisk)
            .filter_by(
                library_item_id=snapshot_item_id,
                format=data.get("format", "qcow2"),
            )
            .order_by(LibraryItemDisk.boot_order)
            .all()
        )
        for sd in snap_disks:
            item = _snapshot_disk_to_cache_item(sd, snapshot_item_id, data)
            if item:
                items.append(item)
    return items


def _filter_shared_cache_items(items_to_cache, host, db_session, pool):
    """Filter out items already on shared storage, coordinate concurrent downloads."""
    items_needing_download = []
    for ic in items_to_cache:
        if ic["cache_path"].startswith("/var/lib/troshka/local/"):
            items_needing_download.append(ic)
            continue
        status, entry = _check_shared_cache(
            db_session,
            pool,
            ic["item_id"],
            "image",
        )
        if status == "ready":
            if _verify_shared_cache_file(host, ic):
                continue
            if entry:
                db_session.delete(entry)
                db_session.commit()
        elif status == "downloading":
            logger.info(
                "  %s being downloaded by another host, waiting...",
                ic["name"],
            )
            if _wait_for_shared_cache(db_session, pool.id, ic["item_id"], "image"):
                logger.info("  %s now available on shared storage", ic["name"])
                continue
            logger.warning("  %s download timed out, will retry", ic["name"])
        rel_path = ic["cache_path"].replace("/var/lib/troshka/shared/", "")
        _create_shared_cache_entry(
            db_session,
            pool,
            ic["item_id"],
            "image",
            rel_path,
        )
        items_needing_download.append(ic)
    return items_needing_download


def _verify_shared_cache_file(host, ic):
    """Check if a shared cache file actually exists on disk."""
    try:
        jid = start_job(host, "/files/stat", {"path": ic["cache_path"]})
        stat_job = wait_for_job(host, jid, timeout=10)
        if stat_job.get("result", {}).get("exists"):
            logger.info("  %s already on shared storage, skipping", ic["name"])
            return True
    except TroshkadError:
        pass
    logger.warning(
        "  %s cache entry says ready but file missing, re-downloading",
        ic["name"],
    )
    return False


def _filter_locally_cached_items(items_to_cache, host):
    """Filter out items already cached locally on the host."""
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
    return items_to_download


def _start_download_jobs(items_to_download, host):
    """Start S3 download jobs on the host for each item. Returns list of active jobs."""
    from app.services import s3_storage
    from app.services.s3_storage import _get_readonly_s3_config, _get_s3_config

    s3_creds = _get_s3_config()
    s3_bucket = s3_storage._bucket()
    central_creds = _get_readonly_s3_config()
    active_jobs = []
    for ic in items_to_download:
        if ic.get("download_creds"):
            dl_creds = ic["download_creds"]
            dl_bucket = dl_creds.get("bucket", s3_bucket)
        elif ic.get("source") == "central" and central_creds:
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
            logger.exception("Failed to start cache job for %s: %s", ic["name"], e)
    return active_jobs


def _format_in_progress_download(name, expected_size, downloaded_gb):
    """Format a progress string for an actively downloading item."""
    total_gb = expected_size / (1024**3) if expected_size else 0
    if downloaded_gb > 0 and total_gb > 0:
        pct = min(99, int(downloaded_gb / total_gb * 100))
        return f"{name}: {downloaded_gb:.1f} / {total_gb:.1f} GB ({pct}%)"
    if total_gb > 0:
        return f"{name}: downloading {total_gb:.1f} GB..."
    return f"{name}: downloading..."


def _build_download_progress_items(active_jobs, completed, failed, host):
    """Build progress display items for active download jobs."""

    items = []
    for aj in active_jobs:
        exp = aj.get("expected_size", 0)
        size_str = f"{exp / (1024**3):.1f} GB" if exp else ""
        if aj["job_id"] in completed:
            items.append(f"{aj['name']}: done{f' ({size_str})' if size_str else ''}")
        elif aj["job_id"] in failed:
            items.append(f"{aj['name']}: failed")
        else:
            downloaded_gb = _get_download_progress_gb(host, aj["job_id"])
            items.append(_format_in_progress_download(aj["name"], exp, downloaded_gb))
    return items


def _get_download_progress_gb(host, job_id):
    """Parse download progress in GB from a cache job's output."""
    from app.services.troshkad_client import poll_job

    try:
        job = poll_job(host, job_id)
        for line in reversed(job.get("output", [])):
            line = line.strip()
            if "Downloading:" in line and "GB" in line:
                try:
                    return float(
                        line.split("Downloading:")[1].strip().replace("GB", "").strip()
                    )
                except (ValueError, IndexError):
                    pass
                break
    except TroshkadError:
        pass
    return 0.0


def _poll_single_download_job(aj, host, completed, failed, is_shared, db_session, pool):
    """Poll a single download job and update completed/failed sets."""
    from app.services.troshkad_client import poll_job

    if aj["job_id"] in completed or aj["job_id"] in failed:
        return
    try:
        job = poll_job(host, aj["job_id"])
    except TroshkadError:
        return
    if job["status"] == "completed":
        completed.add(aj["job_id"])
        logger.info("cache: %s downloaded", aj["name"])
        if is_shared:
            _mark_shared_cache_ready(db_session, pool.id, aj["item_id"], "image")
    elif job["status"] == "failed":
        failed.add(aj["job_id"])
        logger.error(
            "cache: %s failed: %s",
            aj["name"],
            job.get("result", {}).get("error", ""),
        )
        if is_shared:
            _mark_shared_cache_error(db_session, pool.id, aj["item_id"], "image")


def _poll_download_jobs(active_jobs, host, db_session, pool, progress_callback):
    """Poll download jobs until all complete or stall timeout is reached."""

    completed: set[str] = set()
    failed: set[str] = set()
    stale_polls = 0
    last_completed_count = 0
    is_shared = pool and pool.mode.startswith("shared")

    while len(completed) + len(failed) < len(active_jobs):
        _time.sleep(5)
        for aj in active_jobs:
            _poll_single_download_job(
                aj, host, completed, failed, is_shared, db_session, pool
            )

        if progress_callback:
            done_count = len(completed) + len(failed)
            items = _build_download_progress_items(
                active_jobs,
                completed,
                failed,
                host,
            )
            progress_callback(f"{done_count}/{len(active_jobs)}", items)

        if len(completed) + len(failed) == last_completed_count:
            stale_polls += 1
        else:
            stale_polls = 0
            last_completed_count = len(completed) + len(failed)

        if stale_polls >= 720:  # 1 hour with no progress
            logger.error("Download stalled for 1 hour, aborting")
            return failed

    return failed


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
    pool = _get_host_pool(host, db_session)
    nodes = topology.get("nodes", [])

    # Collect all items to cache from topology
    items_to_cache = []
    items_to_cache.extend(_collect_library_items(nodes, db_session, pool))
    items_to_cache.extend(_collect_pxe_boot_isos(nodes, db_session, pool))
    provider_id = getattr(host, "provider_id", None)
    items_to_cache.extend(_collect_pattern_disks(nodes, db_session, pool, provider_id))
    items_to_cache.extend(_collect_snapshot_disks(nodes, db_session))

    # Deduplicate
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
        items_to_cache = _filter_shared_cache_items(
            items_to_cache,
            host,
            db_session,
            pool,
        )

    # Check which items already exist on host (local cache)
    items_to_download = _filter_locally_cached_items(items_to_cache, host)
    if not items_to_download:
        logger.info("  all items cached, no downloads needed")
        return

    # Start and poll download jobs
    active_jobs = _start_download_jobs(items_to_download, host)
    if not active_jobs:
        return

    failed = _poll_download_jobs(
        active_jobs,
        host,
        db_session,
        pool,
        progress_callback,
    )
    if failed:
        logger.error(
            "cache_library_images: %d/%d downloads failed",
            len(failed),
            len(active_jobs),
        )


# ── Async orchestrators ──


def _resolve_network_host_ip(host, db_session, project_id):
    from app.models.mesh_peer import ProjectMeshPeer
    from app.models.project import Project

    project = db_session.query(Project).filter_by(id=project_id).first()
    if project and project.mesh_subnet_id:
        mesh_peers = (
            db_session.query(ProjectMeshPeer).filter_by(project_id=project_id).all()
        )
        peer_ips = [p.wg_address.split("/")[0] for p in mesh_peers]
        this_peer = next((p for p in mesh_peers if p.host_id == host.id), None)
        host_ip = this_peer.wg_address.split("/")[0] if this_peer else host.ip_address
    else:
        all_hosts = db_session.query(Host).filter(Host.state == "active").all()
        peer_ips = [h.ip_address for h in all_hosts if h.ip_address]
        host_ip = host.ip_address
    return host_ip, peer_ips


def _inject_lb_port_forwards(network_config, topology, vni_map):
    lb = network_config.get("loadbalancer")
    if not lb or not lb.get("frontends") or not lb.get("external", True):
        return
    gw = network_config.get("gateway")
    if not gw:
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
    if not gw:
        return
    if gw.get("mode") not in ("nat", "nat-portforward"):
        gw["mode"] = "nat-portforward"
    pf_list = gw.get("port_forwards", [])
    lb_eip_priv = _find_lb_eip_private_ip(lb, gw, topology)
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


def _find_lb_eip_private_ip(lb, gw, topology):
    lb_ext_ip_id = lb.get("ext_ip_id", "")
    if lb_ext_ip_id:
        for eip in topology.get("externalIps", []):
            if eip.get("id") == lb_ext_ip_id and eip.get("_private_ip"):
                return eip["_private_ip"]
    eip_priv_ips = gw.get("eip_private_ips", [])
    return eip_priv_ips[0] if eip_priv_ips else ""


def _setup_networks_via_troshkad(host, topology, vni_map, db_session, project_id):
    """Set up full VXLAN mesh networking via troshkad.

    Builds the network config and sends it to the networks/full-setup endpoint.
    Returns True on success, error string on failure.
    """
    from app.services.vxlan import build_host_network_config

    host_ip, peer_ips = _resolve_network_host_ip(host, db_session, project_id)
    network_config = build_host_network_config(topology, vni_map, peer_ips)
    _inject_lb_port_forwards(network_config, topology, vni_map)

    params = {
        "project_id": project_id,
        "host_ip": host_ip,
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


def _push_mesh_config_to_peer(db, project_id, peer):
    if not peer.host_id:
        return "Peer has no host_id"
    peer_host_id: str = peer.host_id
    host = db.query(Host).filter_by(id=peer_host_id).first()
    if not host:
        return f"Host {peer_host_id[:8]} not found"
    config = get_peer_config_for_host(db, project_id, peer_host_id)
    try:
        job_id = start_job(host, "/mesh/setup", config)
        job = wait_for_job(host, job_id, timeout=60)
        if job["status"] == "failed":
            error_msg = job.get("result", {}).get("error", "unknown")
            return f"Host {host.id[:8]}: {error_msg}"
    except Exception as e:
        return f"Host {host.id[:8]}: {e}"
    return None


def _rollback_mesh(db, project_id, peers):
    for peer in peers:
        host = db.query(Host).filter_by(id=peer.host_id).first()
        if host:
            try:
                troshkad_request(
                    host, "DELETE", f"/mesh/teardown?project_id={project_id}"
                )
            except Exception:
                pass
    delete_mesh_peers(db, project_id)


def _setup_mesh(db, project, host_assignments, host_ips):
    """Push WireGuard configs to all hosts. Returns True on success."""

    peers = create_mesh_peers(
        db,
        project.id,
        host_assignments,
        project.mesh_network_host_id,
        host_ips,
    )

    errors = []
    for peer in peers:
        err = _push_mesh_config_to_peer(db, project.id, peer)
        if err:
            errors.append(err)

    if errors:
        logger.error("Mesh setup failed: %s", errors)
        _rollback_mesh(db, project.id, peers)
        return False
    return True


def _setup_remote_host_network(
    host_id,
    network_host_id,
    network_nodes,
    vni_map,
    all_wg_ips,
    wg_ip_map,
    project_id,
    db,
):
    """Set up VXLAN + bridge on a single remote (non-network) host. Returns error string or None."""
    if host_id == network_host_id:
        return None

    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        return f"Host {host_id[:8]} not found"

    networks = []
    for node in network_nodes:
        vni = vni_map.get(node["id"])
        if vni:
            networks.append(
                {
                    "vni": vni,
                    "bridge_name": f"br-{vni}",
                    "wg_peer_ips": all_wg_ips,
                }
            )

    params = {
        "project_id": project_id,
        "wg_local_ip": wg_ip_map[host_id],
        "networks": networks,
    }
    try:
        job_id = start_job(host, "/mesh/join-network", params)
        job = wait_for_job(host, job_id, timeout=120)
        if job["status"] == "failed":
            error_msg = job.get("result", {}).get("error", "unknown")
            return f"Host {host_id[:8]}: {error_msg}"
    except Exception as e:
        return f"Host {host_id[:8]}: {e}"
    return None


def _setup_remote_networks(db, project, host_assignments, vni_map, topology):
    """Set up VXLAN + bridge on remote (non-network) hosts."""
    from app.models.mesh_peer import ProjectMeshPeer

    network_host_id = project.mesh_network_host_id
    all_peers = db.query(ProjectMeshPeer).filter_by(project_id=project.id).all()
    wg_ip_map = {p.host_id: p.wg_address.split("/")[0] for p in all_peers}
    all_wg_ips = list(wg_ip_map.values())

    network_nodes = [
        n
        for n in topology.get("nodes", [])
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("networkType") != "bmc"
    ]

    errors = []
    for host_id, vm_ids in host_assignments.items():
        err = _setup_remote_host_network(
            host_id,
            network_host_id,
            network_nodes,
            vni_map,
            all_wg_ips,
            wg_ip_map,
            project.id,
            db,
        )
        if err:
            errors.append(err)

    if errors:
        logger.error("Remote network setup failed: %s", errors)
        return False
    return True


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
            logger.exception("PXE setup failed for VNI %s: %s", net["vni"], e)


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
        logger.exception("Seed ISO creation failed: %s", e)


def _resolve_disk_backing(disk, pool=None):
    """Resolve the backing file path for a disk based on its source type."""
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
        return _pattern_cache_path(
            disk["patternId"], _cache_disk_id, disk["format"], pool
        )

    if disk.get("source") == "snapshot" and disk.get("snapshotItemId"):
        from app.core.database import SessionLocal as _SL2
        from app.models.library import LibraryItemDisk as _LID

        _s2 = _SL2()
        _snap_disks = (
            _s2.query(_LID)
            .filter_by(library_item_id=disk["snapshotItemId"], format=disk["format"])
            .order_by(_LID.boot_order)
            .all()
        )
        backing = None
        if _snap_disks:
            s3_key = _snap_disks[0].s3_key
            parts = s3_key.rsplit("/", 1)[-1].rsplit(".", 1)
            orig_disk_id = parts[0] if parts else _snap_disks[0].id
            backing = _snapshot_cache_path(
                disk["snapshotItemId"], orig_disk_id, disk["format"]
            )
        _s2.close()
        return backing

    if disk.get("source") == "library" and disk.get("library_item_id"):
        return _image_cache_path(disk["library_item_id"], disk["format"], pool)

    return None


def _create_vm_disks_via_troshkad(host, project_id, vm, vm_disks, pool=None):
    """Create disk images for a VM via troshkad disks/create. Returns list of job IDs."""
    job_ids = []
    for disk in vm_disks:
        if disk["format"] == "iso":
            continue
        dp = _disk_path(
            project_id, vm["node_id"], disk["node_id"], disk["format"], pool
        )

        backing = _resolve_disk_backing(disk, pool)

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


def _build_vm_disk_list(vm, vm_disks, project_id, pool):
    """Build the disk list for virt-install from topology disk nodes."""
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
        disk_dict = {"path": dp, "bus": disk["bus"]}
        if disk.get("rotation_rate") is not None:
            disk_dict["rotation_rate"] = disk["rotation_rate"]
        disks.append(disk_dict)

    if vm.get("cloud_init"):
        disks.append(
            {
                "path": _seed_path(project_id, vm["node_id"], pool),
                "bus": "sata",
                "device": "cdrom",
            }
        )
    return disks


def _translate_boot_devices(vm, topology):
    """Translate canvas boot device IDs to libvirt boot types."""
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
    return boot_devs


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

    disks = _build_vm_disk_list(vm, vm_disks, project_id, pool)

    networks = []
    for net in vm_networks:
        entry = {"bridge": net["bridge"], "model": net.get("model", "virtio")}
        if net["mac"]:
            entry["mac"] = net["mac"]
        networks.append(entry)

    from app.services.headless import serial_exec_needs_headless

    boot_devs = _translate_boot_devices(vm, topology)

    headless = vm.get("headless")
    if headless is None:
        headless = serial_exec_needs_headless(
            serial_exec_type=vm.get("serial_exec_type", "")
        )

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
        "serial_exec_type": vm.get("serial_exec_type", ""),
    }
    if headless:
        params["headless"] = True
    if vm.get("machine_type"):
        params["machine_type"] = vm["machine_type"]
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

    from app.services.deploy_topology import metadata_bridges_for_topology

    bridges = metadata_bridges_for_topology(topology, vni_map)
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


def _start_ordered_vms(host, project_id, vms, start_order):
    """Start VMs that have explicit start order entries (sequentially).

    Returns (ordered_vm_ids, failed) where ordered_vm_ids is the set of VM IDs
    that were in the start order, and failed is a list of (name, error) tuples.
    """
    ordered_vm_ids = set()
    failed = []
    for entry in start_order:
        vm_id = entry.get("vmId", "")
        vm = next((v for v in vms if v["node_id"] == vm_id), None)
        if not vm:
            continue
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
            logger.warning(_VM_START_FAILED, vm_name, e)
            failed.append((vm["name"], str(e)))
    return ordered_vm_ids, failed


def _start_unordered_vms(host, project_id, vms, ordered_vm_ids, topology):
    """Start VMs not in start order (parallel), skipping powerOnAtDeploy=false.

    Returns list of (name, error) tuples for any VMs that failed.
    """
    power_on_map = {}
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode":
            power_on_map[node["id"]] = node.get("data", {}).get("powerOnAtDeploy", True)

    failed = []
    unordered_jobs = []
    for vm in vms:
        if vm["node_id"] in ordered_vm_ids:
            continue
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
            logger.warning(_VM_START_FAILED, vm_name, e)
            failed.append((vm["name"], str(e)))

    for name, vm_name, job_id in unordered_jobs:
        try:
            wait_for_job(host, job_id, timeout=120)
        except TroshkadError as e:
            logger.warning(_VM_START_FAILED, vm_name, e)
            failed.append((name, str(e)))
    return failed


def _start_vms_via_troshkad(host, project_id, topology):
    """Start VMs respecting start order via troshkad vms/start.
    Returns list of (vm_name, error) for any VMs that failed to start."""
    vms = _extract_vms(topology)
    start_order = topology.get("startOrder", [])

    ordered_vm_ids = set()
    failed = []
    if start_order:
        ordered_vm_ids, failed = _start_ordered_vms(host, project_id, vms, start_order)

    unordered_failed = _start_unordered_vms(
        host, project_id, vms, ordered_vm_ids, topology
    )
    failed.extend(unordered_failed)

    return failed


def _project_deleted(project_id: str) -> bool:
    """Check if a project was deleted or destroy started mid-deploy."""
    from app.core.database import SessionLocal
    from app.models.project import Project

    check_s = SessionLocal()
    try:
        project = check_s.query(Project).filter_by(id=project_id).first()
        if project is None:
            return True
        return project.state == "deleting"
    finally:
        check_s.close()


def _wait_troshkad_job(host, job_id, timeout, what):
    """Poll a troshkad job and raise if it failed."""
    job = wait_for_job(host, job_id, timeout=timeout)
    if job.get("status") == "failed":
        error = job.get("result", {}).get("error", "unknown")
        raise TroshkadError(f"{what} failed: {error}")
    return job


def _troshkad_network_entries(networks: list[dict]) -> list[dict]:
    """Serialize container/pod networks for troshkad create APIs."""
    entries: list[dict] = []
    for n in networks:
        entry: dict = {
            "bridge": n["bridge"],
            "ip": n.get("ip"),
            "mac": n.get("mac"),
            "cidr": n.get("cidr"),
            "gateway": n.get("gateway"),
        }
        if n.get("infra_transit"):
            entry["infra_transit"] = True
        if n.get("dns_nameserver"):
            entry["dns_nameserver"] = n["dns_nameserver"]
        entries.append(entry)
    return entries


def _pod_create_params(host, project_id, ctr, topology, vni_map, pool=None):
    """Build troshkad /pods/create params for a container pod node."""
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

    init_containers = ctr.get("init_containers", [])
    if ctr.get("build_content") is False:
        init_containers = []

    return {
        "project_id": project_id,
        "pod_name": pod_name,
        "networks": _troshkad_network_entries(networks),
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
            for ic in init_containers
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
        "volumes": [
            {
                "disk_path": v["disk_path"],
                "mount_dir": v["mount_dir"],
                "mount_path": v["mount_path"],
            }
            for v in volumes
        ],
        "restart_policy": ctr.get("restart_policy", "always"),
        "privileged": ctr.get("privileged", False),
    }


def _create_container(host, project_id, ctr, topology, vni_map, pool=None):
    """Create a container via troshkad (does not start it)."""
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
        "networks": _troshkad_network_entries(networks),
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
    _wait_troshkad_job(host, job_id, 120, "Container create")
    return container_name


def _start_container(host, container_name):
    """Start a previously created container."""
    job_id = start_job(host, "/containers/start", {"container_name": container_name})
    _wait_troshkad_job(host, job_id, 30, "Container start")


def _create_pod(host, project_id, ctr, topology, vni_map, pool=None):
    """Create a pod via troshkad (does not start it)."""
    create_params = _pod_create_params(host, project_id, ctr, topology, vni_map, pool)
    job_id = start_job(host, "/pods/create", create_params)
    _wait_troshkad_job(host, job_id, 120, "Pod create")
    return f"troshka-{project_id[:8]}-{ctr['name']}"


def _start_pod(host, full_pod_name, timeout=120):
    """Start a previously created pod (runs init containers first)."""
    job_id = start_job(host, "/pods/start", {"pod_name": full_pod_name})
    _wait_troshkad_job(host, job_id, timeout, "Pod start")


def _create_and_start_container(host, project_id, ctr, topology, vni_map, pool=None):
    """Create and start a container via troshkad."""
    container_name = _create_container(host, project_id, ctr, topology, vni_map, pool)
    _start_container(host, container_name)


def _create_and_start_pod(host, project_id, ctr, topology, vni_map, pool=None):
    """Create and start a pod via troshkad."""
    full_pod_name = _create_pod(host, project_id, ctr, topology, vni_map, pool)
    _start_pod(host, full_pod_name)


# --- Ops pod (bastionless / multi-cluster OCP install) ---------------------


def _ocp_clusters(topology) -> list:
    """OCP cluster list from a topology (empty when not an OCP project)."""
    return (topology or {}).get("clusters") or []


def _ops_pod_api_url() -> str:
    """Public Troshka API URL the ops pod calls back to (from `app.external_url`)."""
    try:
        from app.core.config import config as app_config

        app_cfg = getattr(app_config, "app", None)
        return str(getattr(app_cfg, "external_url", "") or "") if app_cfg else ""
    except Exception:
        return ""


def _should_use_ops_pod(topology) -> bool:
    """True when the deploy should create the ops pod instead of the bastion.

    Plan 4b: this is now a per-PROJECT choice. An OCP project (one with
    ``clusters``) uses the ops pod iff its persisted ``ocpInstallVia`` resolves
    to "pod" (the config-backed default) — for single- AND multi-cluster
    projects alike, on ALL host types (troshkad + kubevirt). "bastion" projects
    and non-OCP projects (no ``clusters``) use the bastion path.
    """
    from app.services.template_loader import ocp_install_via

    clusters = _ocp_clusters(topology)
    if not clusters:
        return False
    return ocp_install_via(topology) == "pod"


def _ops_pod_workdir_lines(clusters, workdir) -> list[str]:
    """Bash ``mkdir -p`` lines ensuring the per-cluster workdirs exist.

    Plan 4b, Task 8: the per-cluster install-config/agent-config and the pull
    secret are now delivered as troshkad-mounted read-only files (see
    :func:`app.services.ocp.ops_pod_scaffold.ops_pod_config_files`) — NOT
    base64-echoed into the command — so no secret appears in the pod's ``bash
    -c`` argv / ``podman inspect``. podman auto-creates a bind-mount's parent
    dirs, but we still ``mkdir -p`` each cluster dir so install logs / auth
    output have a writable home even when a cluster has no generated config.
    """
    lines = [f"mkdir -p {workdir}"]
    for cluster in clusters:
        key = str(cluster.get("id") or cluster.get("name") or "cluster")
        lines.append(f"mkdir -p {workdir}/{key}")
    return lines


def _ops_pod_command(clusters, topology, ocp_version, workdir):
    """Full ``bash -c`` argv: ensure workdirs exist, then run the installer.

    The install-runner script (Task 5) reads each cluster's config from
    ``<workdir>/<clusterId>/`` (delivered via mounted files) and installs every
    cluster in parallel, exiting non-zero if any cluster fails. NO secret is
    embedded in this argv.
    """
    from app.services.ocp.ops_pod_install import (
        bmc_for_cluster,
        build_ops_pod_install_script,
    )

    bmc_by_cluster = {}
    for cluster in clusters:
        key = str(cluster.get("id") or cluster.get("name") or "cluster")
        bmc_by_cluster[key] = bmc_for_cluster(topology, cluster)
    script = build_ops_pod_install_script(
        clusters, bmc_by_cluster, ocp_version, workdir
    )
    preamble = "\n".join(_ops_pod_workdir_lines(clusters, workdir))
    return ["bash", "-c", preamble + "\n" + script]


def _ops_pod_create_params(
    project,
    clusters,
    topology,
    vni_map,
    api_url,
    api_key,
    ocp_version,
    pull_secret_json,
):
    """Build the real troshkad ``/pods/create`` params for the ops pod.

    This produces the ACTUAL contract troshkad's ``_handle_pod_create``
    consumes: a ``privileged`` pod named ``ops`` on the infra-transit network
    (ops ``.4``), with one main container on :data:`OPS_POD_IMAGE` whose ``env``
    is a DICT (not ``envVars[]``) and whose ``command`` is a ``bash -c`` argv
    that ensures the per-cluster workdirs exist and then runs the install-runner
    script.

    Plan 4b, Task 8: the per-cluster install-config/agent-config and the pull
    secret are delivered via the troshkad ``files`` capability (written 0600 to
    a per-pod host dir and bind-mounted read-only at their workdir paths) — NOT
    base64-embedded in ``command`` — so no secret appears in the pod argv /
    ``podman inspect``. ``TROSHKA_API_KEY`` remains in ``env`` (acceptable per
    spec §7: env is not exposed in the argv and troshkad's env is not logged).
    """
    from app.services.deploy_topology import _gateway_connected_dns_nameserver
    from app.services.ocp.ops_pod_scaffold import (
        OPS_POD_IMAGE,
        OPS_POD_WORKDIR,
        ops_pod_config_files,
        ops_pod_infra_network,
    )

    project_id = str(getattr(project, "id", ""))
    dns = _gateway_connected_dns_nameserver(topology)
    networks = ops_pod_infra_network(vni_map, dns_nameserver=dns)
    command = _ops_pod_command(clusters, topology, ocp_version, OPS_POD_WORKDIR)
    files = ops_pod_config_files(clusters, OPS_POD_WORKDIR, pull_secret_json)
    container = {
        "name": "ops",
        "image": OPS_POD_IMAGE,
        "cpus": 2,
        "memory": 2048,
        "env": {
            "TROSHKA_API_URL": api_url,
            "TROSHKA_API_KEY": api_key,
            "TROSHKA_PROJECT_ID": project_id,
            "OCP_VERSION": ocp_version,
        },
        "mounts": [],
        "command": command,
        "privileged": True,
    }
    return {
        "project_id": project_id,
        "pod_name": "ops",
        "networks": _troshkad_network_entries(networks),
        "init_containers": [],
        "containers": [container],
        "volumes": [],
        "files": files,
        "restart_policy": "always",
        "privileged": True,
    }


def _deploy_ops_pod(s, host, project_id, project, topology, vni_map):
    """Mint a scoped key and create+start the in-cluster OCP install ops pod.

    Bastionless / multi-cluster path: instead of a bastion VM, an in-cluster ops
    pod runs the agent-based install for every cluster. Only invoked when
    :func:`_should_use_ops_pod` is true. The actual install run inside the pod is
    a live-environment concern; here we mint the project-scoped API key and, per
    host type, create the pod: troshkad ``/pods/create`` (podman) or a native k8s
    Pod on a ``kubevirt-cluster`` host.
    """
    from app.services.ocp.ops_pod_auth import mint_ops_pod_key

    clusters = _ocp_clusters(topology)
    api_key = mint_ops_pod_key(s, project)
    ocp_version = str(clusters[0].get("ocpVersion", "4.20")) if clusters else "4.20"
    logger.info(
        "Deploy %s: creating ops pod for %d cluster(s)",
        project_id[:8],
        len(clusters),
    )
    if host.host_type == "kubevirt-cluster":
        _deploy_ops_pod_kubevirt(
            s, host, project_id, project, topology, clusters, api_key, ocp_version
        )
    else:
        _deploy_ops_pod_troshkad(
            s,
            host,
            project_id,
            project,
            topology,
            vni_map,
            clusters,
            api_key,
            ocp_version,
        )


def _deploy_ops_pod_troshkad(
    s, host, project_id, project, topology, vni_map, clusters, api_key, ocp_version
):
    """troshkad (podman) ops-pod path: shape ``/pods/create`` params, create+start."""
    params = _ops_pod_create_params(
        project,
        clusters,
        topology,
        vni_map,
        api_url=_ops_pod_api_url(),
        api_key=api_key,
        ocp_version=ocp_version,
        pull_secret_json="",
    )
    job_id = start_job(host, "/pods/create", params)
    _wait_troshkad_job(host, job_id, 300, "Ops pod create")
    _start_pod(host, f"troshka-{project_id[:8]}-ops", timeout=300)
    # Pod (bastionless) projects have no ocpMonitor VM node, so the bastion-path
    # `_has_ocp_monitor` gate never sets ocp_status. Mark the install in-progress
    # here so the existing OCP-status UI shows install-in-progress; the ops-pod
    # install monitor drives it to ready/error on completion/failure.
    _mark_ocp_install_started(s, project)
    _start_ops_pod_install_monitor(host, project_id, clusters)


def _deploy_ops_pod_kubevirt(
    s, host, project_id, project, topology, clusters, api_key, ocp_version
):
    """KubeVirt ops-pod path: build Pod+Secret manifests and create them via k8s.

    The pod is attached to BOTH the cluster NAD(s) and the BMC NAD so it can
    serve the agent ISO to the nested VMs and drive their sushy BMCs, mirroring
    the operator's sushy Deployment (``build_bmc_deployment``). The per-cluster
    configs + pull secret ride in a k8s Secret mounted at the install script's
    workdir paths. The live pod run + NAD reachability are **[LIVE-ENV]**.
    """
    from app.services.ocp.ops_pod_scaffold import (
        OPS_POD_WORKDIR,
        build_ops_pod_kubevirt_manifests,
        ops_pod_config_files,
        ops_pod_network_nads,
    )
    from app.services.providers.kubevirt import create_ops_pod

    provider = _ops_pod_provider(s, host)
    command = _ops_pod_command(clusters, topology, ocp_version, OPS_POD_WORKDIR)
    config_files = ops_pod_config_files(clusters, OPS_POD_WORKDIR, "")
    cluster_nads, bmc_nad = ops_pod_network_nads(topology)
    pod, secret = build_ops_pod_kubevirt_manifests(
        namespace=_kubevirt_project_ns(provider, project_id),
        project_id=project_id,
        command=command,
        env={
            "TROSHKA_API_URL": _ops_pod_api_url(),
            "TROSHKA_API_KEY": api_key,
            "TROSHKA_PROJECT_ID": project_id,
            "OCP_VERSION": ocp_version,
        },
        config_files=config_files,
        cluster_nads=cluster_nads,
        bmc_nad=bmc_nad,
    )
    create_ops_pod(provider, project_id, pod, secret)
    _mark_ocp_install_started(s, project)
    _start_ops_pod_install_monitor(host, project_id, clusters)


def _ops_pod_provider(s, host):
    """Resolve the provider row for a host (used by the KubeVirt ops-pod path)."""
    from app.models.provider import Provider

    provider = s.get(Provider, host.provider_id)
    if not provider:
        raise RuntimeError(f"Provider {host.provider_id} not found for ops pod")
    return provider


def _kubevirt_project_ns(provider, project_id: str) -> str:
    from app.services.providers.kubevirt import _project_ns

    return _project_ns(provider, project_id)


def _mark_ocp_install_started(s, project) -> None:
    """Set the initial in-progress OCP status for a pod (bastionless) install.

    Mirrors the bastion path's initial state (see ``_deploy_complete_and_notify``
    / kubevirt deploy) so the SAME OCP-status UI works for pod installs.
    """
    project.ocp_status = "monitoring"
    project.ocp_status_detail = None
    project.ocp_install_elapsed = None
    project.ocp_monitor_started_at = datetime.datetime.now(datetime.UTC)
    s.commit()


def _start_ops_pod_install_monitor(host, project_id: str, clusters: list) -> None:
    """Spawn the ops-pod install-progress monitor as a daemon thread.

    Mirrors :func:`_start_vm_monitor`. The in-cluster ops pod runs the
    agent-based install for every cluster in parallel (restart_policy=always);
    this background monitor tails each cluster's ``install.log``, streams
    per-cluster + aggregate progress to the deploy UI, and — via
    :func:`_ops_pod_running` + ``inject_dead_pod_failures`` — reports ``failed``
    if the pod dies. The poll loop itself is **[LIVE-ENV]**.
    """
    from app.services.ocp.ops_pod_scaffold import OPS_POD_WORKDIR

    threading.Thread(
        target=_monitor_ops_pod_install,
        args=(project_id, host, clusters),
        kwargs={
            "container_name": _ops_pod_container_name(project_id),
            "workdir": OPS_POD_WORKDIR,
        },
        daemon=True,
        name=f"ops-pod-install-{project_id[:8]}",
    ).start()


# ── Ops-pod install progress monitor (Plan 4, Task 7) ──────────────────────
#
# The in-cluster ops pod runs the agent-based install for every cluster in
# parallel, writing each cluster's progress to
# ``<workdir>/<clusterId>/install.log`` (see
# :func:`app.services.ocp.ops_pod_install.build_ops_pod_install_script`). This
# monitor tails those logs via troshkad ``containers/exec``, parses each into an
# install phase, and streams the aggregate + per-cluster status to the deploy
# UI — honoring cancellation. The PURE phase/aggregation logic is
# :func:`ops_pod_install_progress`; everything here (the poll loop, wall-clock
# timing, real ``containers/exec`` log reads, and the cancel-time ops-pod
# destroy) is **[LIVE-ENV]** and not unit-tested.


def _ops_pod_container_name(project_id: str) -> str:
    """troshkad names a pod's main container ``<full_pod_name>-<ctr_name>``; the
    ops pod's main container is ``ops`` (see ``_ops_pod_create_params``)."""
    return f"troshka-{project_id[:8]}-ops-ops"


def _publish_ops_pod_progress(project_id: str, progress: dict) -> None:
    """Stream aggregate + per-cluster ops-pod install progress to the deploy UI."""
    from app.services.ocp.ops_pod_install import ops_pod_progress_items

    overall = progress["overall"]
    failed = progress.get("failed") or []
    detail = (
        f"{len(failed)} cluster(s) failed: {', '.join(failed)}"
        if failed
        else f"install: {overall}"
    )
    _update_deploy_progress(
        project_id,
        f"ocp-install:{overall}",
        detail,
        items=ops_pod_progress_items(progress),
    )


def _exec_ops_pod_cat(host, container_name: str, path: str) -> str | None:
    """[LIVE-ENV] ``cat`` a file inside the ops-pod container via troshkad
    ``containers/exec``; returns None if the file is absent or the exec fails."""
    try:
        job_id = start_job(
            host,
            "/containers/exec",
            {"container_name": container_name, "command": ["cat", path]},
        )
        job = wait_for_job(host, job_id, timeout=30)
        if job.get("status") == "completed":
            return (job.get("result") or {}).get("stdout", "")
    except TroshkadError:
        pass
    return None


def _ops_pod_running(host, container_name: str) -> bool:
    """[LIVE-ENV] Whether the ops-pod container reports a running state.

    Uses troshkad's batch ``/containers/states``. Conservative on uncertainty:
    if the batch call errors (returns None — e.g. a transient host blip) we
    assume the pod is still running, so a network hiccup never forces a false
    ``failed``. Only a container that is present-but-not-``running`` or genuinely
    absent from a successful listing counts as dead (→ dead-job injection).
    """
    from app.services.troshkad_client import get_all_container_states

    states = get_all_container_states(host)
    if states is None:
        return True
    info = states.get(container_name)
    if info is None:
        return False
    return str(info.get("state", "")).lower() == "running"


def _read_ops_pod_cluster_logs(
    host, container_name: str, cluster_keys: list[str], workdir: str
) -> dict[str, str]:
    """[LIVE-ENV] Read every cluster's ``install.log`` from the ops pod.

    A missing/unreadable log maps to ``""`` (→ ``creating-image``), so a cluster
    that hasn't produced output yet still appears as in-progress.
    """
    return {
        key: (
            _exec_ops_pod_cat(host, container_name, f"{workdir}/{key}/install.log")
            or ""
        )
        for key in cluster_keys
    }


# Consecutive confirmed "not running" polls before the monitor declares the ops
# pod dead and fails its non-terminal clusters. The ops pod is
# ``restart_policy=always`` and the install script is idempotent (Task 4 skips a
# cluster that already has a kubeconfig), so a pod that OOMs/reboots is EXPECTED
# to resume on restart. During that restart window a successful
# ``/containers/states`` call briefly reports the container absent or
# ``created``/``restarting`` — a single such observation must NOT fail the
# deploy. Requiring 3 CONSECUTIVE confirmed-not-running polls at the default
# 15s ``poll_interval`` tolerates a ~30s restart window while still failing a
# genuine crash-loop promptly (well before the 2h timeout). A transient troshkad
# error (``_ops_pod_running`` returns True conservatively) resets the counter.
_OPS_POD_DEAD_POLLS = 3


def _next_ops_pod_dead_count(count: int, pod_running: bool) -> int:
    """Pure: advance the consecutive not-running poll counter.

    Reset to 0 while the pod is running (``_ops_pod_running`` also returns True on
    a transient status-call error, so those never increment); otherwise +1.
    """
    return 0 if pod_running else count + 1


def _cancel_ops_pod_install(host, project_id: str, cluster_keys) -> None:
    """Stop a cancelled bastionless OCP install by destroying the ops pod.

    The real per-cluster install runs INSIDE the persistent ``restart_policy=
    always`` ops pod; the ``/pods/create`` job that launched it completes
    immediately, so cancelling that job is a no-op. Destroying the pod is what
    actually halts the in-pod install (and prevents it from restarting). The
    destroy path (Task 6 of Plan 4) revokes the scoped ops-pod key, so we do not
    double-handle key revocation here. Best-effort: a troshkad failure is logged,
    never raised, and the terminal ``cancelled`` status is still published.
    """
    from app.services.ocp.ops_pod_install import ops_pod_install_progress

    pod_name = f"troshka-{project_id[:8]}-ops"
    try:
        job_id = start_job(
            host,
            "/pods/destroy",
            {"pod_name": pod_name, "project_id": project_id, "volumes": []},
        )
        wait_for_job(host, job_id, timeout=60)
    except TroshkadError as e:
        logger.warning(
            "Ops pod %s: failed to destroy ops pod on cancel: %s",
            project_id[:8],
            e,
        )
    progress = ops_pod_install_progress(
        {key: "" for key in cluster_keys}, cancelled=True
    )
    _publish_ops_pod_progress(project_id, progress)


def _ops_pod_overall_to_ocp_status(overall: str) -> str | None:
    """Map a terminal ops-pod install phase to the project ``ocp_status`` vocab.

    Pod (bastionless) projects have no ocpMonitor VM node, so the bastion-path
    ``maybe_start_ocp_health_monitor`` gate never fires; the ops-pod install
    monitor must drive the SAME ``ocp_status`` fields the UI reads. This mirrors
    the bastion VM monitor's terminal vocabulary: ``complete`` -> ``ready``
    (success), ``failed``/``timeout`` -> ``error`` (failure). ``cancelled`` is a
    user action, not an install outcome, so it returns None (ocp_status left
    untouched — the destroy path handles project teardown).
    """
    if overall == "complete":
        return "ready"
    if overall in ("failed", "timeout"):
        return "error"
    return None


def _finalize_ops_pod_ocp_status(
    project_id: str, overall: str, elapsed_secs: int
) -> None:
    """Persist ``ocp_status``/``ocp_install_elapsed`` for a terminal pod install.

    So the existing OCP-status UI reflects a bastionless install's outcome; a
    non-outcome phase (e.g. ``cancelled``) is a no-op.
    """
    status = _ops_pod_overall_to_ocp_status(overall)
    if status:
        _ocp_update_status(project_id, status, elapsed_secs)


def _monitor_ops_pod_install(
    project_id: str,
    host,
    clusters: list[dict],
    container_name: str | None = None,
    workdir: str | None = None,
    poll_interval: int = 15,
    timeout: int = 7200,
) -> str:
    """[LIVE-ENV loop] Poll per-cluster install.log and stream install progress.

    Loops until every cluster reaches a terminal phase (``complete``/``failed``),
    the project is cancelled, or ``timeout`` elapses. Each iteration reads the
    live logs, maps them to phases via :func:`ops_pod_install_progress`, and
    publishes the aggregate + per-cluster status. On cancellation it destroys the
    ops pod (via :func:`_cancel_ops_pod_install`, which actually halts the in-pod
    install) and marks the status cancelled. Returns the terminal overall phase
    (``complete``/``failed``/``cancelled``/``timeout``).
    """
    import time as _t

    from app.services.ocp.ops_pod_install import (
        _cluster_key as _ops_cluster_key,
    )
    from app.services.ocp.ops_pod_install import (
        inject_dead_pod_failures,
        ops_pod_install_progress,
    )
    from app.services.ocp.ops_pod_scaffold import OPS_POD_WORKDIR

    workdir = workdir or OPS_POD_WORKDIR
    container_name = container_name or _ops_pod_container_name(project_id)
    cluster_keys = [_ops_cluster_key(c) for c in clusters]
    start = _t.time()
    deadline = start + timeout
    dead_count = 0

    while _t.time() < deadline:
        if _is_deploy_cancelled(project_id):
            _cancel_ops_pod_install(host, project_id, cluster_keys)
            return "cancelled"
        per_cluster = _read_ops_pod_cluster_logs(
            host, container_name, cluster_keys, workdir
        )
        # Dead-job detection: a crashed pod can never finish a non-terminal
        # cluster. But the pod is restart_policy=always + idempotent, so a brief
        # restart window is recoverable — only fail after _OPS_POD_DEAD_POLLS
        # CONSECUTIVE confirmed-not-running polls (transient status errors reset
        # the counter, see _ops_pod_running / _next_ops_pod_dead_count).
        dead_count = _next_ops_pod_dead_count(
            dead_count, _ops_pod_running(host, container_name)
        )
        per_cluster = inject_dead_pod_failures(
            per_cluster, pod_running=dead_count < _OPS_POD_DEAD_POLLS
        )
        progress = ops_pod_install_progress(per_cluster)
        _publish_ops_pod_progress(project_id, progress)
        if progress["done"]:
            _finalize_ops_pod_ocp_status(
                project_id, progress["overall"], int(_t.time() - start)
            )
            return progress["overall"]
        _t.sleep(poll_interval)

    logger.warning("Ops pod %s: install monitor timed out", project_id[:8])
    _finalize_ops_pod_ocp_status(project_id, "timeout", int(_t.time() - start))
    return "timeout"


def _sync_deployed_container_node(project, container_id: str, topo: dict) -> None:
    """Update the deployed_topology's container node (+ showroom meta) to match
    the current topology after a container redeploy, so the canvas snapshot
    reflects the newly-applied config (e.g. a changed showroom content_repo)."""
    deployed = copy.deepcopy(project.deployed_topology or {})
    cur_node = next(
        (n for n in topo.get("nodes", []) if n.get("id") == container_id), None
    )
    if not cur_node:
        return
    replaced = False
    for i, node in enumerate(deployed.get("nodes", [])):
        if node.get("id") == container_id:
            deployed["nodes"][i] = copy.deepcopy(cur_node)
            replaced = True
            break
    if replaced and "showroom" in topo:
        deployed["showroom"] = copy.deepcopy(topo["showroom"])
    if replaced:
        project.deployed_topology = deployed


def redeploy_container_bg(project_id: str, container_id: str) -> None:
    """Redeploy a single container/pod node: destroy then recreate so its init
    containers re-run (re-clone content_repo/ref and rebuild). VMs are left
    untouched. Used e.g. to pull updated showroom content after the repo changes.
    """
    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.services.deploy_topology import _extract_containers

    prog_key = f"redeploy-container:{project_id}:{container_id}"
    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(id=project_id).first()
        if not project:
            return
        host = (
            db.query(Host).filter_by(id=project.host_id).first()
            if project.host_id
            else None
        )
        topo = copy.deepcopy(project.topology or {})
        ctr = next(
            (c for c in _extract_containers(topo) if c["node_id"] == container_id), None
        )
        if not host or not ctr:
            set_progress(
                prog_key,
                {"step": "error", "detail": "Container or host not found"},
            )
            return
        name = ctr.get("name", "container")
        pool = _get_host_pool(host, db)
        vni_map = project.vni_map or {}
        set_progress(prog_key, {"step": "redeploy", "detail": f"Recreating {name}..."})
        _destroy_container(host, project_id, ctr, topo, pool)
        if ctr.get("is_pod"):
            _create_and_start_pod(host, project_id, ctr, topo, vni_map, pool)
        else:
            _create_and_start_container(host, project_id, ctr, topo, vni_map, pool)
        _sync_deployed_container_node(project, container_id, topo)
        db.commit()
        notify_project(
            project_id,
            {"type": "container-redeployed", "containerId": container_id},
        )
        logger.info(
            "Redeploy container %s/%s: complete", project_id[:8], container_id[:8]
        )
    except Exception as e:  # noqa: BLE001 - report failure via progress
        logger.exception(
            "Redeploy container %s/%s failed", project_id[:8], container_id[:8]
        )
        try:
            set_progress(prog_key, {"step": "error", "detail": str(e)})
        except Exception:
            pass
    finally:
        delete_progress(prog_key)
        db.close()


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


def _library_disk_virtual_size_bytes(db, lib_item, s3_path: str) -> int:
    """Return qcow2 virtual size for a library disk (bytes), or 0 if unknown."""
    from sqlalchemy import select

    from app.models.library import LibraryItemDisk

    disks = list(
        db.scalars(select(LibraryItemDisk).filter_by(library_item_id=lib_item.id)).all()
    )
    for disk in disks:
        if s3_path and disk.s3_key != s3_path:
            continue
        if disk.virtual_size_bytes:
            return disk.virtual_size_bytes
        # Raw images (e.g. ISOs) have no qcow2 virtual size but a real file
        # size — fall back to size_bytes so goldens/clones are sized correctly.
        if disk.size_bytes:
            return disk.size_bytes
    if len(disks) == 1 and disks[0].virtual_size_bytes:
        return disks[0].virtual_size_bytes
    if len(disks) == 1 and disks[0].size_bytes:
        return disks[0].size_bytes

    max_gb = 0
    for disk in (lib_item.vm_config or {}).get("disks", []):
        size = disk.get("size") or disk.get("size_gb") or 0
        max_gb = max(max_gb, int(size))
    if max_gb:
        return max_gb * 1073741824
    # ISOs and other single-file items have no LibraryItemDisk rows or vm_config
    # disks, but the LibraryItem itself records the real file size.
    if getattr(lib_item, "size_bytes", 0):
        return lib_item.size_bytes
    return 0


def library_item_deploy_size_gb(lib_item, db=None) -> int:
    """Size in GB for deploy/PVC sizing — virtual disk size, not sparse file size."""
    import math

    virtual_bytes = 0
    if db is not None:
        virtual_bytes = _library_disk_virtual_size_bytes(
            db, lib_item, lib_item.s3_key or ""
        )
    elif lib_item.item_disks:
        virtual_bytes = max((d.virtual_size_bytes or 0) for d in lib_item.item_disks)
    if not virtual_bytes:
        for disk in (lib_item.vm_config or {}).get("disks", []):
            size = disk.get("size") or disk.get("size_gb") or 0
            virtual_bytes = max(virtual_bytes, int(size) * 1073741824)
    if virtual_bytes:
        return max(1, math.ceil(virtual_bytes / (1024**3)))
    return max(1, (lib_item.size_bytes or 0) // (1024**3))


def _apply_virtual_size_to_disk_data(data, virtual_size_bytes: int) -> None:
    """Bump topology disk size when image virtual size exceeds the template."""
    import math

    if not virtual_size_bytes:
        return
    real_gb = math.ceil(virtual_size_bytes / (1024**3))
    data["sourceSizeGb"] = real_gb
    if real_gb > (data.get("size", 0) or 0):
        data["size"] = real_gb


def _qcow2_virtual_size_from_s3(client, bucket, key, op) -> int:
    """Return a qcow2 image's virtual size (bytes) by reading its header from S3.

    Reads only the first 72 bytes via a ranged GET — the qcow2 magic is at
    offset 0 and the virtual disk size is a big-endian u64 at offset 24. Returns
    0 when the object is unreachable, not a qcow2 (ISO/raw), or truncated, so
    callers fall back to the recorded size and never fail a deploy on this.
    """
    import struct

    if client is None:
        return 0
    try:
        resp = client.get_object(
            Bucket=bucket, Key=key, Range="bytes=0-71", **(op or {})
        )
        header = resp["Body"].read()
    except Exception:
        return 0
    if len(header) < 32 or header[:4] != b"QFI\xfb":
        return 0
    return struct.unpack(">Q", header[24:32])[0]


def _measure_disk_virtual_size(key, candidates) -> int:
    """Measure a disk's qcow2 virtual size from the first reachable S3 source.

    ``candidates`` is a list of ``(client, bucket, op)`` tuples tried in order;
    returns the first qcow2 size found, or 0 if none are reachable/qcow2.
    """
    for client, bucket, op in candidates:
        size = _qcow2_virtual_size_from_s3(client, bucket, key, op)
        if size:
            return size
    return 0


def _persist_library_disk_size(db, lib_item, s3_path, measured) -> None:
    """Heal a LibraryItemDisk's recorded virtual size after measuring from S3."""
    from sqlalchemy import select

    from app.models.library import LibraryItemDisk

    disks = list(
        db.scalars(select(LibraryItemDisk).filter_by(library_item_id=lib_item.id)).all()
    )
    match = next((d for d in disks if s3_path and d.s3_key == s3_path), None)
    if match is None and len(disks) == 1:
        match = disks[0]
    if match is not None and match.virtual_size_bytes != measured:
        match.virtual_size_bytes = measured
        db.add(match)


def _resolve_pattern_disk(
    data,
    db,
    target_provider_id,
    s3_client=None,
    bucket=None,
    s3_op=None,
    central_s3_client=None,
    central_bucket=None,
    central_op=None,
):
    """Resolve source (obc|central) + S3 path for a pattern-sourced disk.

    Uses PatternLocation on the target cluster. Raises DeployError if the disk
    is not synced anywhere reachable from that cluster (placement should
    prevent this; this is the correctness backstop).

    When an S3 client can reach the resolved object, the disk's true qcow2
    virtual size is measured from the image header and used for sizing (and
    written back to the PatternDisk row), correcting stale/nominal capture
    metadata. Unreachable objects fall back to the recorded size.
    """
    from sqlalchemy import select

    from app.models.pattern import PatternDisk as PatternDiskModel
    from app.services.pattern_locations import pattern_disk_source_for_cluster

    pid = data["patternId"]
    pattern_disk_id = data.get("patternDiskId", "")
    pd_record = None
    if pattern_disk_id:
        pd_record = db.scalars(
            select(PatternDiskModel).filter_by(id=pattern_disk_id, pattern_id=pid)
        ).first()

    s3_path = (
        pd_record.s3_key
        if pd_record and pd_record.s3_key
        else f"patterns/{pid}/{pattern_disk_id}.qcow2"
    )

    source = pattern_disk_source_for_cluster(db, pattern_disk_id, target_provider_id)
    if source is None:
        label = data.get("label", pattern_disk_id[:8])
        raise DeployError(
            f"pattern disk {label} is not available on the target cluster"
            f" — storage not ready"
        )

    data["resolvedS3Path"] = s3_path
    data["diskSource"] = source
    data["centralSource"] = False
    logger.info(
        "Deploy: pattern disk %s s3=%s source=%s",
        data.get("label", "?"),
        s3_path[:40],
        source,
    )
    measured = _measure_disk_virtual_size(
        s3_path,
        [
            (s3_client, bucket, s3_op),
            (central_s3_client, central_bucket, central_op),
        ],
    )
    if measured:
        _apply_virtual_size_to_disk_data(data, measured)
        if pd_record and pd_record.virtual_size_bytes != measured:
            pd_record.virtual_size_bytes = measured
            db.add(pd_record)
    elif pd_record and pd_record.virtual_size_bytes:
        _apply_virtual_size_to_disk_data(data, pd_record.virtual_size_bytes)


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
    if lib_item:
        candidate = (
            (central_s3_client, central_bucket, central_op)
            if use_central
            else (s3_client, bucket, s3_op)
        )
        measured = _measure_disk_virtual_size(s3_path, [candidate])
        if measured:
            _apply_virtual_size_to_disk_data(data, measured)
            _persist_library_disk_size(db, lib_item, s3_path, measured)
        else:
            virtual_bytes = _library_disk_virtual_size_bytes(db, lib_item, s3_path)
            _apply_virtual_size_to_disk_data(data, virtual_bytes)
    logger.info(
        "Deploy: disk %s s3=%s central=%s",
        data.get("label", "?"),
        s3_path[:40],
        use_central,
    )


def _resolve_disk_s3_paths(
    topology,
    db,
    target_provider_id,
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
        _ensure_storage_library_ref(node, db)
        if data.get("source") == "pattern" and data.get("patternId"):
            _resolve_pattern_disk(
                data,
                db,
                target_provider_id,
                s3_client=s3_client,
                bucket=bucket,
                s3_op=s3_op,
                central_s3_client=central_s3_client,
                central_bucket=central_bucket,
                central_op=central_op,
            )
        elif data.get("source") in ("library", "snapshot") and data.get(
            "libraryItemId"
        ):
            # Snapshot nodes carry a libraryItemId whose LibraryItem.s3_key is
            # already correct: data disks -> snapshots/<id>/<disk>.qcow2, and
            # ISOs (not captured) -> the original library item's real key.
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


def _preflight_verify_library_disks(
    topology,
    s3_client,
    bucket,
    s3_op,
    central_s3_client,
    central_bucket,
    central_op,
):
    """HEAD central-only library disks before deploy."""
    if not central_s3_client:
        return
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") != "storageNode":
            continue
        if data.get("source") not in ("library", "snapshot"):
            continue
        if not data.get("centralSource"):
            continue
        key = data.get("resolvedS3Path", "")
        if not key:
            continue
        try:
            central_s3_client.head_object(Bucket=central_bucket, Key=key, **central_op)
        except Exception as exc:
            label = data.get("label", key[:16])
            raise DeployError(
                f"library disk {label} not found in central S4 ({key})"
            ) from exc


def _preflight_verify_pattern_disks(topology, s3_client, bucket, s3_op):
    """HEAD every central-source pattern disk against central S4 before deploy.

    OBC-source disks are trusted (their synced PatternLocation was written only
    after a verified capture, and the OBC endpoint is unreachable from here).
    If s3_client is None (central S4 not configured), central-disk checks are skipped.
    """
    if not s3_client:
        return
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") != "storageNode":
            continue
        if data.get("source") != "pattern":
            continue
        if data.get("diskSource") != "central":
            continue
        key = data.get("resolvedS3Path", "")
        try:
            s3_client.head_object(Bucket=bucket, Key=key, **s3_op)
        except Exception as exc:
            label = data.get("label", key[:16])
            raise DeployError(
                f"pattern disk {label} not found in central S4 — storage not ready"
            ) from exc


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
    return f"{friendly}: importing image"


def _dv_running_error(dv) -> str | None:
    """Return CDI Running/Error message when a DV is stuck with a hidden failure."""
    for cond in dv.get("status", {}).get("conditions", []):
        if cond.get("type") == "Running" and cond.get("reason") == "Error":
            msg = cond.get("message", "")
            if msg:
                return msg
    return None


def _format_dv_status_line(friendly, dv):
    """Format a single DataVolume into a human-readable status line."""
    err = _dv_running_error(dv)
    if err:
        short_err = err[:60]
        return f"{friendly}: error — {short_err}"

    dv_phase = dv.get("status", {}).get("phase", "")
    dv_progress = dv.get("status", {}).get("progress", "N/A")

    if dv_phase == "Succeeded":
        return f"{friendly}: done"
    if dv_phase == "ImportInProgress":
        return _format_import_progress(friendly, dv, dv_progress)
    if dv_phase == "CloneInProgress":
        return f"{friendly}: cloning"
    if dv_phase == "CloneScheduled":
        return f"{friendly}: waiting to clone"
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


def _dv_status_suffix(line: str) -> str:
    return line.split(": ", 1)[1] if ": " in line else line


def _pick_disk_dv_status(friendly, cache_dv, clone_dv) -> str:
    """Pick the deploy status to show for a disk across cache and clone DVs."""
    if clone_dv:
        clone_phase = clone_dv.get("status", {}).get("phase", "")
        if clone_phase == "Succeeded":
            return _dv_status_suffix(_format_dv_status_line(friendly, clone_dv))
        if clone_phase == "CloneInProgress":
            return _dv_status_suffix(_format_dv_status_line(friendly, clone_dv))
        if clone_phase in ("CloneScheduled", "Pending", "ImportScheduled") and cache_dv:
            cache_phase = cache_dv.get("status", {}).get("phase", "")
            if cache_phase and cache_phase != "Succeeded":
                return _dv_status_suffix(_format_dv_status_line(friendly, cache_dv))
            if cache_phase == "Succeeded":
                return "waiting to clone"
        if clone_phase == "Failed":
            return _dv_status_suffix(_format_dv_status_line(friendly, clone_dv))
    if cache_dv:
        return _dv_status_suffix(_format_dv_status_line(friendly, cache_dv))
    if clone_dv:
        return _dv_status_suffix(_format_dv_status_line(friendly, clone_dv))
    return "waiting"


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
        clone_by_label: dict[str, dict] = {}
        golden_ref_map: dict[str, str] = {}
        for dv in all_dvs:
            if dv["metadata"]["namespace"] == "troshka-cache":
                continue
            raw_name = dv["metadata"]["name"]
            friendly = clone_name_map.get(raw_name)
            if not friendly:
                continue
            label = friendly[:24]
            clone_by_label[label] = dv
            pvc_src = dv.get("spec", {}).get("source", {}).get("pvc", {})
            if pvc_src.get("namespace") == "troshka-cache":
                golden_name = pvc_src.get("name", "")
                if golden_name:
                    golden_ref_map[golden_name] = label

        cache_by_label: dict[str, dict] = {}
        for dv in all_dvs:
            if dv["metadata"]["namespace"] != "troshka-cache":
                continue
            raw_name = dv["metadata"]["name"]
            friendly = golden_name_map.get(raw_name) or golden_ref_map.get(raw_name)
            if not friendly:
                continue
            cache_by_label[friendly[:24]] = dv

        all_labels = set(cache_by_label) | set(clone_by_label)
        best_status = {
            label: _pick_disk_dv_status(
                label, cache_by_label.get(label), clone_by_label.get(label)
            )
            for label in all_labels
        }
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


def _notify_client_topology_update(project_id, project, db):
    """Push hydrated topology (incl. gateway routes) to connected clients."""
    from app.api.projects import _client_topology_snapshot
    from app.services.ws_pubsub import notify_project

    notify_project(
        project_id,
        {
            "type": "topology-update",
            "topology": _client_topology_snapshot(project, db=db),
        },
    )


def _finalize_kubevirt_deploy(project_id, project, topology, db, host=None):
    """Handle the Running phase — update project state and topology."""
    from app.services.deploy_topology import inject_showroom_gateway_port_forwards
    from app.services.ws_pubsub import notify_project

    db.refresh(project)
    inject_showroom_gateway_port_forwards(topology, project.vni_map or {}, "kubevirt")

    project.state = "active"
    clean_topo = copy.deepcopy(topology)
    for node in clean_topo.get("nodes", []):
        ndata = node.get("data", {})
        ndata.pop("resolvedS3Path", None)
        ndata.pop("presignedUrl", None)
        ndata.pop("ciGeneratedUserData", None)
        if node.get("type") == "vmNode" and not ndata.get("bmcIp"):
            ndata["bmcIp"] = ""

    _deploy_create_provider_routes(
        db, project_id, clean_topo, host=host, project=project
    )
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
    _notify_client_topology_update(project_id, project, db)
    notify_project(project_id, {"type": "project-state", "state": "active"})
    logger.info("Deploy %s: kubevirt deploy complete", project_id[:8])


def _find_gateway_port_forwards(topology, canvas_id, provider_type=None):
    from app.services.deploy_topology import _ROUTE_PROVIDERS

    route_web = provider_type in _ROUTE_PROVIDERS
    for node in topology.get("nodes", []):
        node_data = node.get("data", {})
        if node_data.get("subtype") == "gateway":
            return [
                pf
                for pf in node_data.get("portForwards", [])
                if pf.get("extIpId") == canvas_id
                # On OpenShift-ingress providers 443/80 are served by Routes, never
                # the EIP LB. Cloud providers keep them on the EIP.
                and not (route_web and str(pf.get("extPort")) in ("443", "80"))
            ]
    return []


def _resolve_eip_provider(project_id, project, db):
    from app.models.host import Host
    from app.models.provider import Provider
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
        return None, None, None
    if not provider and host:
        provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        logger.warning("Deploy %s: no provider for EIP allocation", project_id[:8])
        return None, None, None
    driver = get_provider_driver(provider)
    return provider, host, driver


def _allocate_single_kubevirt_eip(
    project_id, ext_ip, provider, host, driver, topology, db
):
    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import allocate_eip, associate_eip

    canvas_id = ext_ip.get("id", "")
    existing = (
        db.query(ElasticIp)
        .filter_by(project_id=project_id, canvas_eip_id=canvas_id)
        .first()
    )
    eip = (
        existing
        if existing
        else allocate_eip(db, provider, project_id, canvas_id, host)
    )
    if eip.state != "associated":
        associate_eip(db, eip, host)
    ext_ip["ip"] = eip.public_ip

    pf_for_eip = _find_gateway_port_forwards(topology, canvas_id, provider.type)
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


def _allocate_kubevirt_eips(project_id, project, topology, db):
    """Allocate MetalLB EIPs for a kubevirt native project after operator deploy."""
    provider, host, driver = _resolve_eip_provider(project_id, project, db)
    if not provider:
        return

    external_ips = topology.get("externalIps", []) or []
    if external_ips:
        logger.info(
            "Deploy %s: allocating %d MetalLB EIPs", project_id[:8], len(external_ips)
        )

        for ext_ip in external_ips:
            try:
                canvas_id = ext_ip.get("id", "")
                if _should_skip_route_eip(provider, topology, canvas_id, project_id):
                    _clear_route_only_eip(db, project_id, canvas_id, ext_ip)
                    continue
                _allocate_single_kubevirt_eip(
                    project_id, ext_ip, provider, host, driver, topology, db
                )
            except Exception:
                logger.exception(
                    "Deploy %s: EIP allocation failed for %s (non-fatal)",
                    project_id[:8],
                    ext_ip.get("id", "")[:8],
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
    from app.services.deploy_topology import _is_showroom_node, is_showroom_infra_ip
    from app.services.providers.kubevirt import ensure_showroom_cluster_service

    showroom_node = next(
        (n for n in topology.get("nodes", []) if _is_showroom_node(n)),
        None,
    )
    showroom_svc = ""
    if showroom_node:
        try:
            showroom_svc = ensure_showroom_cluster_service(
                provider, project_id, showroom_node["id"]
            )
        except Exception:
            logger.exception(
                "Deploy %s: failed to create showroom service (non-fatal)",
                project_id[:8],
            )

    all_forwards = []
    for node in topology.get("nodes", []):
        node_data = node.get("data", {})
        if node_data.get("subtype") == "gateway":
            for pf in node_data.get("portForwards", []):
                ext_port = pf.get("extPort", "")
                int_ip = pf.get("intIp", "")
                if showroom_svc and is_showroom_infra_ip(int_ip):
                    int_ip = showroom_svc
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
            host = None
            if project.host_id:
                from app.models.host import Host

                host = db.query(Host).filter_by(id=project.host_id).first()
            _finalize_kubevirt_deploy(project_id, project, topology, db, host=host)
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


def _generate_exec_ssh_keypair(project_id):
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
    return exec_privkey_pem, exec_pubkey


def _regenerate_kubevirt_cloud_init(topology, project, exec_pubkey):
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


def _wait_for_namespace_termination(provider, project_id):
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    _, _kc_core, _ = _get_k8s_clients(provider)
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


def _replace_stale_kubevirt_cr(provider, project_id):
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    try:
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

    _ca, _cv1, _ac = _get_k8s_clients(provider)
    _ns = _project_ns(provider, project_id)
    cr_name = f"project-{project_id[:8]}"

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

    _wait_for_old_kubevirt_resources(_ca, _cv1, _ns, cr_name, project_id)


def _wait_for_old_kubevirt_resources(_ca, _cv1, _ns, cr_name, project_id):
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
                group=_KUBEVIRT_API,
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

    try:
        _resolve_disk_s3_paths(
            topology,
            db,
            host.provider_id,
            s3_client,
            bucket,
            s3_op,
            central_s3_client,
            central_bucket,
            central_op,
        )
        _preflight_verify_pattern_disks(topology, s3_client, bucket, s3_op)
        _preflight_verify_library_disks(
            topology,
            s3_client,
            bucket,
            s3_op,
            central_s3_client,
            central_bucket,
            central_op,
        )
    except DeployError as e:
        project.state = "error"
        project.deploy_error = str(e)
        db.commit()
        logger.warning("Deploy %s aborted: %s", project_id[:8], e)
        return

    exec_privkey_pem, exec_pubkey = _generate_exec_ssh_keypair(project_id)
    _regenerate_kubevirt_cloud_init(topology, project, exec_pubkey)

    _update_deploy_progress(project_id, "networks", "creating operator resources")
    notify_project(
        project_id,
        {
            "type": "deploy-progress",
            "step": "networks",
            "detail": "creating operator resources",
        },
    )

    _wait_for_namespace_termination(provider, project_id)

    existing_cr = None
    cr_name = f"project-{project_id[:8]}"
    try:
        existing_cr = driver.get_project_status(provider, project_id)
    except Exception:
        pass

    _resume_poll = False
    if existing_cr and existing_cr.get("phase") == "Deploying":
        _resume_poll = True
        logger.info("Deploy %s: CR already deploying, resuming poll", project_id[:8])
    elif existing_cr and existing_cr.get("phase"):
        logger.info(
            "Deploy %s: replacing stale CR with fresh presigned URLs",
            project_id[:8],
        )
        _replace_stale_kubevirt_cr(provider, project_id)

    if not _resume_poll:
        from app.services.deploy_topology import inject_showroom_gateway_port_forwards

        if inject_showroom_gateway_port_forwards(
            topology, project.vni_map or {}, "kubevirt"
        ):
            project.topology = topology
            db.commit()
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
                db=db,
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


def _build_multihost_assignments(project):
    raw_assignments = project.host_assignments or {}
    if not raw_assignments:
        return None
    host_assignments: dict[str, list[str]] = {}
    for vm_id, hid in raw_assignments.items():
        host_assignments.setdefault(hid, []).append(vm_id)
    return host_assignments or None


def _resolve_multihost_ips(host_assignments, db):
    hosts_in_mesh = []
    for hid in host_assignments:
        h = db.query(Host).filter_by(id=hid).first()
        if h:
            hosts_in_mesh.append(h)
    same_pool = (
        len({h.storage_pool_id for h in hosts_in_mesh if h.storage_pool_id}) <= 1
    )
    host_ips = {}
    for h in hosts_in_mesh:
        if same_pool and h.private_ip:
            host_ips[h.id] = h.private_ip
        else:
            host_ips[h.id] = h.ip_address
    return host_ips


def _deploy_vms_on_host(host, project_id, project, host_vms, topology, vni_map, db):
    pool = _get_host_pool(host, db)
    host_label = f"{host.ip_address} ({len(host_vms)} VMs)"
    vm_node_ids = {vm["node_id"] for vm in host_vms}

    _update_deploy_progress(project_id, "images", f"caching images on {host_label}")
    logger.info("Deploy %s: caching images on %s", project_id[:8], host_label)
    _prepare_topology_library_refs(topology, db, project)
    cache_library_images(topology, host, db)

    _update_deploy_progress(project_id, "seeds", f"creating seed ISOs on {host_label}")
    logger.info("Deploy %s: creating seeds on %s", project_id[:8], host_label)
    host_topo = _filter_topology_for_host(topology, vm_node_ids)
    _create_seed_isos_via_troshkad(host, project_id, host_topo, pool)

    err = _create_multihost_disks(
        host, project_id, host_vms, topology, pool, host_label
    )
    if err:
        return err

    _clean_stale_domains(host, project_id, host_vms, host_label)

    clock_offset = None
    if project.clock_target:
        from app.services.clock_service import compute_clock_offset

        clock_offset = compute_clock_offset(project.clock_target)

    return _define_multihost_vms(
        host, project_id, host_vms, topology, vni_map, pool, clock_offset, host_label
    )


def _create_multihost_disks(host, project_id, host_vms, topology, pool, host_label):
    _update_deploy_progress(project_id, "disks", f"creating disks on {host_label}")
    logger.info("Deploy %s: creating disks on %s", project_id[:8], host_label)
    disk_jobs = []
    for vm in host_vms:
        vm_disks = _find_vm_disks(vm["node_id"], topology)
        job_ids = _create_vm_disks_via_troshkad(host, project_id, vm, vm_disks, pool)
        disk_jobs.extend(job_ids if isinstance(job_ids, list) else [])
    for jid in disk_jobs:
        job = wait_for_job(host, jid, timeout=900)
        if job.get("status") == "failed":
            error = job.get("result", {}).get("error", "unknown")
            return f"Disk creation failed on {host_label}: {error}"
    return None


def _clean_stale_domains(host, project_id, host_vms, host_label):
    _update_deploy_progress(project_id, "vms", f"defining VMs on {host_label}")
    logger.info("Deploy %s: defining VMs on %s", project_id[:8], host_label)
    for vm in host_vms:
        domain_name = _vm_domain_name(project_id, vm["node_id"])
        try:
            j = start_job(host, "/vm/info", {"name": domain_name})
            r = wait_for_job(host, j, timeout=10)
            if r.get("result", {}).get("state"):
                logger.info(
                    "Deploy %s: stale domain %s on %s, removing",
                    project_id[:8],
                    domain_name,
                    host_label,
                )
                try:
                    d = start_job(host, _VMS_DESTROY_PATH, {"domain_name": domain_name})
                    wait_for_job(host, d, timeout=60)
                except TroshkadError:
                    pass
        except TroshkadError:
            pass


def _define_multihost_vms(
    host, project_id, host_vms, topology, vni_map, pool, clock_offset, host_label
):
    for vm in host_vms:
        job_id = _create_vm_via_troshkad(
            host,
            project_id,
            vm,
            topology,
            vni_map,
            pool,
            clock_offset=clock_offset,
        )
        if not job_id:
            continue
        job = wait_for_job(host, job_id, timeout=300)
        if job.get("status") == "failed":
            error = job.get("result", {}).get("error", "unknown")
            return f"VM creation failed on {host_label}: {error}"
        dom_uuid = job.get("result", {}).get("domain_uuid", "")
        if dom_uuid:
            for n in topology.get("nodes", []):
                if n["id"] == vm["node_id"]:
                    n.setdefault("data", {})["domainUuid"] = dom_uuid
                    break
    return None


def _start_multihost_vms(project_id, vm_id_set_by_host, topology, db):
    _update_deploy_progress(project_id, "starting", "starting VMs")
    logger.info(
        "Deploy %s: starting VMs across %d hosts",
        project_id[:8],
        len(vm_id_set_by_host),
    )
    for host_id, vm_node_ids in vm_id_set_by_host.items():
        host = db.query(Host).filter_by(id=host_id).first()
        if not host:
            continue
        host_topo = _filter_topology_for_host(topology, vm_node_ids)
        start_failures = _start_vms_via_troshkad(host, project_id, host_topo)
        if start_failures:
            failed_names = ", ".join(name for name, _ in start_failures)
            logger.warning(
                "Deploy %s: some VMs failed to start on %s: %s",
                project_id[:8],
                host.ip_address,
                failed_names,
            )


def _deploy_multihost(project_id: str, project, db):
    """Multi-host deploy orchestration: mesh → networks → VMs per host."""
    logger.info("Deploy %s: starting multi-host orchestration", project_id[:8])

    host_assignments = _build_multihost_assignments(project)
    if not host_assignments:
        logger.error("Deploy %s: no host assignments found", project_id[:8])
        project.state = "error"
        project.deploy_error = "No host assignments found for multi-host deploy"
        db.commit()
        return

    host_ips = _resolve_multihost_ips(host_assignments, db)
    topology = project.topology or {}
    vni_map = project.vni_map or {}

    _update_deploy_progress(project_id, "mesh", "setting up WireGuard mesh")
    logger.info(
        "Deploy %s: setting up mesh across %d hosts",
        project_id[:8],
        len(host_assignments),
    )
    if not _setup_mesh(db, project, host_assignments, host_ips):
        project.state = "error"
        project.deploy_error = "Mesh setup failed"
        db.commit()
        _delete_deploy_progress(project_id)
        return

    network_host_id = project.mesh_network_host_id
    network_host = db.query(Host).filter_by(id=network_host_id).first()
    if not network_host:
        logger.error(
            "Deploy %s: network host %s not found", project_id[:8], network_host_id[:8]
        )
        project.state = "error"
        project.deploy_error = "Network host not found"
        db.commit()
        _delete_deploy_progress(project_id)
        return

    _update_deploy_progress(
        project_id, "networks", "setting up networks on network host"
    )
    logger.info(
        "Deploy %s: setting up networks on network host %s",
        project_id[:8],
        network_host_id[:8],
    )
    with _get_network_lock(network_host.id):
        net_result = _setup_networks_via_troshkad(
            network_host, topology, vni_map, db, project_id
        )
    if net_result is not True:
        logger.error("Deploy %s: network setup failed: %s", project_id[:8], net_result)
        project.state = "error"
        project.deploy_error = f"Network setup failed: {net_result}"
        db.commit()
        _delete_deploy_progress(project_id)
        return

    _update_deploy_progress(
        project_id, "remote-networks", "setting up VXLAN on remote hosts"
    )
    logger.info("Deploy %s: setting up VXLAN on remote hosts", project_id[:8])
    if not _setup_remote_networks(db, project, host_assignments, vni_map, topology):
        project.state = "error"
        project.deploy_error = "Remote network setup failed"
        db.commit()
        _delete_deploy_progress(project_id)
        return

    all_vms = _extract_vms(topology)
    vm_id_set_by_host: dict[str, set[str]] = {}
    for vm_node_id, hid in (project.host_assignments or {}).items():
        vm_id_set_by_host.setdefault(hid, set()).add(vm_node_id)

    for host_id, vm_node_ids in vm_id_set_by_host.items():
        host = db.query(Host).filter_by(id=host_id).first()
        if not host:
            continue
        host_vms = [v for v in all_vms if v["node_id"] in vm_node_ids]
        err = _deploy_vms_on_host(
            host, project_id, project, host_vms, topology, vni_map, db
        )
        if err:
            project.state = "error"
            project.deploy_error = err
            db.commit()
            _delete_deploy_progress(project_id)
            return

    project.topology = topology
    db.commit()

    _start_multihost_vms(project_id, vm_id_set_by_host, topology, db)

    project.state = "active"
    project.deploy_error = None
    project.deployed_topology = topology
    db.commit()
    _delete_deploy_progress(project_id)
    logger.info("Deploy %s: multi-host deploy complete", project_id[:8])
    notify_project(project_id, {"type": "project-state", "state": "active"})


_ROUTE_ACCESS_PORTS = frozenset({80, 443, 6443})


def _should_skip_route_eip(provider, topology, canvas_id, project_id):
    """Skip MetalLB/EIP allocation when all forwards use OCP Routes (ocpvirt/kubevirt)."""
    if provider.type not in ("ocpvirt", "kubevirt"):
        return False
    pf_ports = set()
    for node in topology.get("nodes", []):
        node_data = node.get("data", {})
        if node_data.get("subtype") == "gateway":
            for pf in node_data.get("portForwards", []):
                if pf.get("extIpId") == canvas_id:
                    pf_ports.add(int(pf.get("extPort", 0)))
            break
    if pf_ports and pf_ports.issubset(_ROUTE_ACCESS_PORTS):
        logger.info(
            "Deploy %s: skipping EIP for %s — all ports (%s) handled by Routes",
            project_id[:8],
            canvas_id[:8],
            pf_ports,
        )
        return True
    return False


def _should_skip_ocpvirt_eip(provider, topology, canvas_id, project_id):
    """Backward-compatible alias."""
    return _should_skip_route_eip(provider, topology, canvas_id, project_id)


def _clear_route_only_eip(db, project_id, canvas_id, ext_ip):
    """Release MetalLB EIP when all forwards use OCP Routes instead."""
    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import release_eip

    existing = (
        db.query(ElasticIp)
        .filter_by(project_id=project_id, canvas_eip_id=canvas_id)
        .first()
    )
    if existing:
        try:
            release_eip(db, existing)
            logger.info(
                "Deploy %s: released route-only EIP %s",
                project_id[:8],
                canvas_id[:8],
            )
        except Exception:
            logger.exception(
                "Deploy %s: failed to release route-only EIP %s (non-fatal)",
                project_id[:8],
                canvas_id[:8],
            )
    for key in ("ip", "_private_ip", "_transit_port_map", "state"):
        ext_ip.pop(key, None)


def _allocate_single_eip(s, provider, project_id, host, ext_ip, topology):
    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import (
        allocate_eip,
        allocate_transit_ports,
        associate_eip,
    )
    from app.services.providers import get_provider_driver

    canvas_id = ext_ip.get("id", "")
    existing = (
        s.query(ElasticIp)
        .filter_by(project_id=project_id, canvas_eip_id=canvas_id)
        .first()
    )
    eip = (
        existing if existing else allocate_eip(s, provider, project_id, canvas_id, host)
    )
    if eip.state != "associated":
        associate_eip(s, eip, host)
    ext_ip["ip"] = eip.public_ip
    ext_ip["_private_ip"] = eip.private_ip

    if provider.type != "ec2" and not eip.port_map:
        pf_for_eip = _find_gateway_port_forwards(topology, canvas_id, provider.type)
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
                        "name": f"pf-{ep}",
                    }
                    for ep, tp in port_map.items()
                ],
            )
    if eip.port_map:
        ext_ip["_transit_port_map"] = eip.port_map


def _deploy_allocate_eips(s, project_id, project, host, topology, external_ips):
    _checkpoint(s, project_id, "eips")
    _update_deploy_progress(project_id, "eips", "allocating elastic IPs")
    logger.info("Deploy %s: allocating %d EIPs", project_id[:8], len(external_ips))

    from app.models.provider import Provider

    provider = (
        s.query(Provider).filter_by(id=project.provider_id).first()
        if project.provider_id
        else None
    )
    if not provider and host.provider_id:
        provider = s.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return "No provider configured for EIP allocation"

    for ext_ip in external_ips:
        canvas_id = ext_ip.get("id", "")
        if _should_skip_route_eip(provider, topology, canvas_id, project_id):
            _clear_route_only_eip(s, project_id, canvas_id, ext_ip)
            ext_ip["_skip"] = True
            continue
        _allocate_single_eip(s, provider, project_id, host, ext_ip, topology)

    for ext_ip in external_ips:
        ext_ip.pop("_skip", None)
    project.topology = topology
    s.commit()
    notify_project(
        project_id,
        {"type": "external-ips-updated", "externalIps": external_ips},
    )
    return None


def _deploy_setup_lb(host, project_id, topology, vni_map):
    from app.services.vxlan import build_host_network_config as _build_net_config

    _net_config = _build_net_config(topology, vni_map, [])
    lb_config = _net_config.get("loadbalancer")
    if not lb_config or not lb_config.get("frontends"):
        return lb_config
    _update_deploy_progress(project_id, "load balancer", "starting HAProxy")
    logger.info("Deploy %s: setting up load balancer", project_id[:8])
    ns = f"troshka-{project_id[:8]}"
    lb_ip = lb_config.get("lb_ip", "")
    if not lb_ip:
        net_list = _net_config.get("networks", [])
        if net_list:
            import ipaddress as _ipa

            first_cidr = net_list[0].get("dhcp_config", {}).get("gateway", "")
            if first_cidr:
                try:
                    lb_ip = str(_ipa.IPv4Address(first_cidr) + 1)
                except ValueError:
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
    return lb_config


def _collect_gateway_sg_rules(gateway_node, project_id):
    """Collect security group rules from gateway port forwards."""
    rules = []
    if (
        gateway_node
        and gateway_node.get("data", {}).get("gatewayMode") == "nat-portforward"
    ):
        for pf in gateway_node.get("data", {}).get("portForwards", []):
            if pf.get("extPort"):
                rules.append(
                    {
                        "project_id": project_id,
                        "ext_port": int(pf["extPort"]),
                        "protocol": "tcp",
                    }
                )
    return rules


def _deploy_sync_sg_rules(s, project_id, project, host, topology, lb_config):
    from app.models.provider import Provider as _Prov
    from app.services.eip_service import sync_security_group_rules

    _provider = (
        s.query(_Prov).filter_by(id=project.provider_id).first()
        if project.provider_id
        else None
    )
    if not _provider and host.provider_id:
        _provider = s.query(_Prov).filter_by(id=host.provider_id).first()
    if not _provider:
        return
    gateway_node = next(
        (
            n
            for n in topology.get("nodes", [])
            if n.get("type") == "networkNode"
            and n.get("data", {}).get("subtype") == "gateway"
        ),
        None,
    )
    desired_sg = _collect_gateway_sg_rules(gateway_node, project_id)
    if lb_config and lb_config.get("frontends") and lb_config.get("external", True):
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


def _find_gateway_connected_network(topology, gateway_id):
    """Find the network node connected to a gateway node."""
    nodes_by_id = {n["id"]: n for n in topology.get("nodes", [])}
    for edge in topology.get("edges", []):
        if edge.get("source") == gateway_id:
            target = nodes_by_id.get(edge.get("target", ""))
            if target and target.get("type") == "networkNode":
                return target
    return None


def _find_gateway_ip(topology):
    """Find the gateway IP from the topology by locating the gateway node's connected network."""
    import ipaddress

    gateway = next(
        (n for n in topology.get("nodes", []) if n.get("type") == "gatewayNode"), None
    )
    if not gateway:
        return None
    net_node = _find_gateway_connected_network(topology, gateway["id"])
    if not net_node:
        return None
    cidr = net_node.get("data", {}).get("cidr", "192.168.1.0/24")
    network = ipaddress.ip_network(cidr, strict=False)
    return str(network.network_address + 1)


def _deploy_inject_gateway_ip(topology, project_id):
    gateway_ip = _find_gateway_ip(topology)
    if gateway_ip:
        for node in topology.get("nodes", []):
            if node.get("type") == "vmNode" and node.get("data", {}).get("cloudInit"):
                node["data"]["gateway_ip"] = gateway_ip
        logger.info(
            "Deploy %s: injected gateway_ip %s into VM cloud-init data",
            project_id[:8],
            gateway_ip,
        )


def _lookup_transit_port(topology, pf) -> int | None:
    """Return EIP transit port for a gateway port forward, if allocated."""
    ext_ip_id = pf.get("extIpId", "")
    ext_port = str(pf.get("extPort", ""))
    for ext_ip in topology.get("externalIps", []):
        if ext_ip.get("id") != ext_ip_id:
            continue
        port_map = ext_ip.get("_transit_port_map") or {}
        tp = port_map.get(ext_port)
        return int(tp) if tp is not None else None
    return None


def _create_routes_for_gateway(
    s, driver, provider, host, project_id, node_data, topology
):
    """Create OCP Routes for routable port forwards and return endpoint list."""
    from app.services.deploy_topology import is_ops_infra_ip, is_showroom_infra_ip
    from app.services.eip_service import allocate_standalone_transit_port

    external_endpoints = []
    showroom_route = None
    for pf in node_data.get("portForwards", []):
        ext_port = int(pf.get("extPort", 0))
        if ext_port not in _ROUTE_ACCESS_PORTS:
            continue
        int_ip = pf.get("intIp", "")
        int_port = int(pf.get("intPort", ext_port))
        vm_name = _find_vm_name_by_ip(topology, int_ip)
        try:
            if provider.type == "ocpvirt":
                transit_port = _lookup_transit_port(topology, pf)
                # Infra transit pods (showroom .3, ops .4) always need their own
                # DNAT target.
                setup_dnat = (
                    transit_port is None
                    or is_showroom_infra_ip(int_ip)
                    or is_ops_infra_ip(int_ip)
                )
                if transit_port is None:
                    transit_port = allocate_standalone_transit_port(s, host)
                result = driver.create_route_access(
                    provider,
                    host,
                    project_id,
                    vm_name,
                    int_ip,
                    ext_port,
                    int_port,
                    transit_port=transit_port,
                    setup_dnat=setup_dnat,
                )
            else:
                result = driver.create_route_access(
                    provider,
                    host,
                    project_id,
                    vm_name,
                    int_ip,
                    ext_port,
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
            if is_showroom_infra_ip(int_ip):
                showroom_route = result
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
    _create_app_proxy_routes(driver, provider, project_id, topology, showroom_route)
    return external_endpoints


def _create_app_proxy_routes(driver, provider, project_id, topology, showroom_route):
    """Create a public route per app-proxy host (console/oauth) cloning the showroom
    route, and fill the console tab URL. No-op unless the showroom has proxy_hosts."""
    from app.services.showroom_scaffold import (
        app_proxy_internal_hosts,
        app_proxy_public_host,
        derive_apps_domain,
    )

    if not showroom_route or not hasattr(driver, "create_app_proxy_route"):
        return
    showroom_node = next(
        (n for n in topology.get("nodes", []) if n.get("data", {}).get("isShowroom")),
        None,
    )
    if not showroom_node:
        return
    hosts = app_proxy_internal_hosts(showroom_node["data"].get("showroomTabs", []))
    if not hosts:
        return
    apps_domain = derive_apps_domain(showroom_route.get("hostname", ""))
    src_route = showroom_route.get("route_name")
    if not apps_domain or not src_route:
        return
    for internal in hosts:
        public = app_proxy_public_host(project_id, internal, apps_domain)
        try:
            driver.create_app_proxy_route(provider, project_id, public, src_route)
            logger.info(
                "Deploy %s: app-proxy route %s → %s", project_id[:8], public, internal
            )
        except Exception:
            logger.warning(
                "Deploy %s: app-proxy route failed for %s",
                project_id[:8],
                internal,
                exc_info=True,
            )
    _fill_showroom_app_proxy_urls(showroom_node, project_id, apps_domain)


def _fill_showroom_app_proxy_urls(showroom_node, project_id, apps_domain):
    """Substitute the __TROSHKA_APP_PROXY__ placeholder in the showroom pod's baked
    ui-config (UI_CONFIG_B64 init env) with the deterministic public host URL."""
    import base64

    from app.services.showroom_scaffold import fill_app_proxy_tab_urls

    for ic in showroom_node.get("data", {}).get("initContainers", []):
        for ev in ic.get("envVars", []):
            if ev.get("key") != "UI_CONFIG_B64":
                continue
            try:
                ui = base64.b64decode(ev["value"]).decode()
            except Exception:
                continue
            filled = fill_app_proxy_tab_urls(ui, project_id, apps_domain)
            if filled != ui:
                ev["value"] = base64.b64encode(filled.encode()).decode()


def _resolve_project_provider(s, host, project):
    from app.models.host import Host
    from app.models.provider import Provider

    if host and host.provider_id:
        provider = s.query(Provider).filter_by(id=host.provider_id).first()
        if provider:
            return provider
    if project and project.provider_id:
        provider = s.query(Provider).filter_by(id=project.provider_id).first()
        if provider:
            return provider
    if project and project.host_id:
        project_host = s.query(Host).filter_by(id=project.host_id).first()
        if project_host and project_host.provider_id:
            return s.query(Provider).filter_by(id=project_host.provider_id).first()
    return None


def _deploy_create_provider_routes(s, project_id, topology, host=None, project=None):
    """Create OCP Routes for routable port forwards on ocpvirt and kubevirt native."""
    provider = _resolve_project_provider(s, host, project)
    if not provider or provider.type not in ("ocpvirt", "kubevirt"):
        return
    from app.services.providers import get_provider_driver

    driver = get_provider_driver(provider)
    for node in topology.get("nodes", []):
        node_data = node.get("data", {})
        if node_data.get("subtype") != "gateway":
            continue
        external_endpoints = _create_routes_for_gateway(
            s, driver, provider, host, project_id, node_data, topology
        )
        if external_endpoints:
            node_data["externalEndpoints"] = external_endpoints


def _deploy_create_ocpvirt_routes(s, host, project_id, topology):
    """Backward-compatible wrapper."""
    _deploy_create_provider_routes(s, project_id, topology, host=host)


def _detect_pattern_id(topology):
    """Return the pattern ID from the first storage node, or None."""
    for node in topology.get("nodes", []):
        if node.get("type") == "storageNode":
            pattern_id = node.get("data", {}).get("patternId")
            if pattern_id:
                return pattern_id
    return None


def _deploy_pull_container_images(host, project_id, topology, s):
    containers = _extract_containers(topology)
    logger.info(
        "Deploy %s: found %d containers to pull", project_id[:8], len(containers)
    )
    if not containers:
        return
    is_pattern_deploy = _is_pattern_deploy(topology)
    pattern_id = _detect_pattern_id(topology) if is_pattern_deploy else None

    _update_deploy_progress(
        project_id, step="container_pull", detail="Pulling container images..."
    )
    logger.info("Deploy %s: pulling container images", project_id[:8])
    for ctr in containers:
        if ctr.get("is_pod"):
            _pull_pod_images(host, ctr, s)
            continue
        if not ctr["image"]:
            continue
        if is_pattern_deploy and pattern_id:
            _load_container_from_pattern(host, project_id, ctr, pattern_id)
        else:
            _pull_single_container_image(host, ctr, s)


def _pull_pod_images(host, ctr, s):
    all_images = set()
    for ic in ctr.get("init_containers", []):
        if ic.get("image"):
            all_images.add(ic["image"])
    for pc in ctr.get("pod_containers", []):
        if pc.get("image"):
            all_images.add(pc["image"])
    for img in all_images:
        pull_params = {"image": img}
        _add_registry_creds(pull_params, ctr, s)
        job_id = start_job(host, "/containers/pull", pull_params)
        wait_for_job(host, job_id, timeout=600)


def _pull_single_container_image(host, ctr, s):
    pull_params = {"image": ctr["image"]}
    _add_registry_creds(pull_params, ctr, s)
    job_id = start_job(host, "/containers/pull", pull_params)
    wait_for_job(host, job_id, timeout=600)


def _add_registry_creds(pull_params, ctr, s):
    cred_id = ctr.get("registry_credential_id")
    if not cred_id:
        return
    from app.core.encryption import decrypt
    from app.models.registry_credential import RegistryCredential

    cred = s.query(RegistryCredential).filter_by(id=cred_id).first()
    if cred:
        pull_params["registry"] = cred.registry_url
        pull_params["username"] = cred.username
        pull_params["password"] = decrypt(cred.password)


def _load_container_from_pattern(host, project_id, ctr, pattern_id):
    from app.services.s3_storage import _bucket, _get_s3_config

    tar_filename = f"container-{ctr['node_id'][:8]}-image.tar.gz"
    cache_path = f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{tar_filename}"
    s3_key = f"patterns/{pattern_id}/{tar_filename}"
    creds = _get_s3_config()
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
    logger.info(
        "Deploy %s: loading container image %s from cache",
        project_id[:8],
        ctr["image"],
    )
    job_id = start_job(host, "/containers/load-image", {"input_path": cache_path})
    wait_for_job(host, job_id, timeout=300)


def _deploy_validate_bmc(project_id, topology):
    _ = project_id
    bmc_network_exists = any(
        n.get("type") == "networkNode" and n.get("data", {}).get("networkType") == "bmc"
        for n in topology.get("nodes", [])
    )
    if not bmc_network_exists:
        return None
    missing_bmc_ips = [
        n["data"].get("name", n["id"][:8])
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode"
        and n.get("data", {}).get("bmcEnabled")
        and not n.get("data", {}).get("bmcIp")
    ]
    if missing_bmc_ips:
        return f"BMC-enabled VMs missing BMC IP: {', '.join(missing_bmc_ips)}"
    return None


def _deploy_create_disks(host, project_id, topology, pool):
    vms = _extract_vms(topology)
    disk_items = _build_disk_progress_items(vms)
    _update_deploy_progress(
        project_id, "creating disks", "preparing VM disks", items=disk_items
    )
    disk_jobs = []
    for vm in vms:
        vm_disks = _find_vm_disks(vm["node_id"], topology)
        job_ids = _create_vm_disks_via_troshkad(host, project_id, vm, vm_disks, pool)
        disk_jobs.extend(job_ids if isinstance(job_ids, list) else [])
    containers = _extract_containers(topology)
    for ctr in containers:
        ctr_vols = _find_container_volumes(ctr["node_id"], topology, project_id, pool)
        for vol in ctr_vols:
            disk_node = next(
                (n for n in topology.get("nodes", []) if n["id"] == vol["node_id"]),
                None,
            )
            disk_data = (disk_node or {}).get("data", {})
            disk_info = {
                "source": disk_data.get("source"),
                "patternId": disk_data.get("patternId"),
                "patternDiskId": disk_data.get("patternDiskId"),
                "format": disk_data.get("format", "raw"),
                "node_id": vol["node_id"],
            }
            params = {
                "path": vol["disk_path"],
                "size_gb": vol["size_gb"],
                "format": "raw",
            }
            backing = _resolve_disk_backing(disk_info, pool)
            if backing:
                params["backing_file"] = backing
            jid = start_job(host, "/disks/create", params)
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
            logger.exception("Deploy %s: disk creation failed: %s", project_id[:8], e)
            raise
    return vms


def _detect_recert_from_pattern(s, topology):
    """Check if the pattern referenced by any storage node has recert enabled."""
    for node in topology.get("nodes", []):
        if node.get("type") == "storageNode":
            pid = node.get("data", {}).get("patternId")
            if pid:
                pat = s.query(Pattern).filter_by(id=pid).first()
                return bool(pat and pat.recert)
    return None


def _detect_common_password(topology):
    """Find common_password from the first cloud-init VM that has one set."""
    for n in topology.get("nodes", []):
        if n.get("type") == "vmNode" and n.get("data", {}).get("cloudInit"):
            pw = n.get("data", {}).get("ciCloudUserPassword")
            if pw:
                return pw
    return None


def _resolve_recert_settings(s, topology):
    """Resolve deploy_recert and common_password from topology markers or pattern DB."""
    deploy_recert = topology.pop("_deploy_recert", None)
    common_password = topology.pop("_deploy_common_password", None)
    if deploy_recert is None:
        deploy_recert = _detect_recert_from_pattern(s, topology)
    if not common_password:
        common_password = _detect_common_password(topology)
    return deploy_recert, common_password


def _auto_enable_recert_on_rhcos(topology, deploy_recert, project_id):
    """Auto-enable recert on RHCOS VMs when pattern has recert enabled."""
    from app.services.ocp_topology_flags import apply_sno_ocp_vm_flags, rhcos_vms

    if len(rhcos_vms(topology)) == 1:
        apply_sno_ocp_vm_flags(topology, recert=bool(deploy_recert))
        if deploy_recert:
            logger.info(
                "Deploy %s: auto-enabled OCP flags on SNO RHCOS VM from pattern",
                project_id[:8],
            )
        return
    if not deploy_recert or deploy_recert is False:
        return
    has_recert_vm = any(
        n.get("type") == "vmNode" and n.get("data", {}).get("recertEnabled")
        for n in topology.get("nodes", [])
    )
    if not has_recert_vm:
        for n in topology.get("nodes", []):
            if n.get("type") == "vmNode" and n.get("data", {}).get("os") == "rhcos":
                n.setdefault("data", {})["recertEnabled"] = True
        logger.info(
            "Deploy %s: auto-enabled recert on RHCOS VMs from pattern",
            project_id[:8],
        )


def _deploy_handle_recert(s, host, project_id, topology, pool):
    if not (_is_pattern_deploy(topology) and _is_ocp_topology(topology)):
        return
    _update_deploy_progress(project_id, "certs", "regenerating certificates")
    deploy_recert, common_password = _resolve_recert_settings(s, topology)
    _auto_enable_recert_on_rhcos(topology, deploy_recert, project_id)
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


def _build_vm_progress_items(vms, current_index):
    """Build progress item list showing defined/defining/pending status for each VM."""
    items = []
    for vj, v in enumerate(vms):
        n = v.get("name", v["node_id"][:8])
        if vj < current_index:
            items.append(f"{n}: defined")
        elif vj == current_index:
            items.append(f"{n}: defining...")
        else:
            items.append(f"{n}: pending")
    return items


def _build_disk_progress_items(vms):
    """Build progress items for the disk-creation phase (runs before defining).

    Disks are created in parallel up front, so every VM shows 'creating disks'
    together — distinct from the later per-VM 'defining' phase so the UI does
    not conflate slow disk work with domain definition.
    """
    return [f"{v.get('name', v['node_id'][:8])}: creating disks..." for v in vms]


def _clean_stale_domain(host, project_id, domain_name):
    """Check for and remove a stale libvirt domain before re-creating it."""
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
                j = start_job(host, _VMS_DESTROY_PATH, {"domain_name": domain_name})
                wait_for_job(host, j, timeout=60)
            except TroshkadError:
                pass
    except TroshkadError:
        pass


def _define_single_vm(
    host, project_id, vm, topology, vni_map, pool, disk_cache, clock_offset
):
    """Define a single VM via troshkad and capture its domain UUID."""
    domain_name = f"troshka-{project_id[:8]}-{vm['node_id'][:8]}"
    _clean_stale_domain(host, project_id, domain_name)
    job_id = _create_vm_via_troshkad(
        host, project_id, vm, topology, vni_map, pool, disk_cache, clock_offset
    )
    if not job_id:
        return
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


def _deploy_define_vms(
    host, project_id, vms, topology, vni_map, pool, disk_cache, clock_offset
):
    for vi, vm in enumerate(vms):
        items = _build_vm_progress_items(vms, vi)
        _update_deploy_progress(
            project_id, "creating VMs", f"{vi}/{len(vms)}", items=items
        )
        try:
            _define_single_vm(
                host, project_id, vm, topology, vni_map, pool, disk_cache, clock_offset
            )
        except TroshkadError as e:
            logger.exception("Deploy %s: VM creation failed: %s", project_id[:8], e)
            raise


def _deploy_setup_bmc(host, project_id, topology):
    has_bmc_vms = any(
        n.get("type") == "vmNode" and n.get("data", {}).get("bmcEnabled")
        for n in topology.get("nodes", [])
    )
    bmc_config = _extract_bmc_config(topology, project_id)
    if has_bmc_vms and not bmc_config:
        return "VMs have BMC enabled but no BMC network (type: bmc) is defined", None
    if not bmc_config:
        return None, None
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
        logger.error("Deploy %s: BMC setup failed: %s", project_id[:8], bmc_result)
        return f"BMC setup failed: {bmc_result}", None
    return None, bmc_config


def _create_ordered_containers(
    host, project_id, containers, start_order, topology, vni_map, pool
):
    """Create containers that appear in start_order. Returns set of ordered IDs."""
    ordered_ids = set()
    for entry in start_order:
        if entry.get("entryType") != "container":
            continue
        ctr_id = entry.get("containerId", entry.get("vmId", ""))
        ctr = next((c for c in containers if c["node_id"] == ctr_id), None)  # type: ignore[arg-type]
        if not ctr:
            continue
        ordered_ids.add(ctr_id)
        if ctr.get("is_pod"):
            _create_pod(host, project_id, ctr, topology, vni_map, pool)
        else:
            _create_container(host, project_id, ctr, topology, vni_map, pool)
    return ordered_ids


def _deploy_create_containers(host, project_id, topology, vni_map, pool):
    containers = _extract_containers(topology)
    logger.info(
        "Deploy %s: found %d containers to create", project_id[:8], len(containers)
    )
    if not containers:
        return
    _update_deploy_progress(
        project_id, step="containers", detail="Creating containers..."
    )
    logger.info("Deploy %s: creating containers", project_id[:8])
    start_order = topology.get("startOrder", [])
    ordered_ids = _create_ordered_containers(
        host, project_id, containers, start_order, topology, vni_map, pool
    )
    for ctr in containers:
        if ctr["node_id"] not in ordered_ids:
            if ctr.get("is_pod"):
                _create_pod(host, project_id, ctr, topology, vni_map, pool)
            else:
                _create_container(host, project_id, ctr, topology, vni_map, pool)


def _start_ordered_containers(
    host, project_id, containers, start_order, topology, auto_start
):
    """Start containers from start_order after VMs, respecting delays."""
    started_ids = set()
    for entry in start_order:
        if entry.get("entryType") != "container":
            continue
        if not auto_start or entry.get("autoStart", True) is False:
            continue
        ctr_id = entry.get("containerId", entry.get("vmId", ""))
        ctr = next((c for c in containers if c["node_id"] == ctr_id), None)  # type: ignore[arg-type]
        if not ctr:
            continue
        started_ids.add(ctr_id)
        delay = entry.get("delaySeconds", 0)
        if delay > 0:
            _time.sleep(delay)
        if ctr.get("is_pod"):
            full_pod_name = f"troshka-{project_id[:8]}-{ctr['name']}"
            timeout = 900 if ctr.get("build_content", True) else 120
            _start_pod(host, full_pod_name, timeout=timeout)
        else:
            container_name = f"troshka-{project_id[:8]}-{ctr['node_id'][:8]}"
            _start_container(host, container_name)
        for node in topology.get("nodes", []):
            if node["id"] == ctr_id:
                node.setdefault("data", {})["status"] = "running"
                break
    return started_ids


def _deploy_start_containers(host, project_id, topology, auto_start):
    containers = _extract_containers(topology)
    if not containers or not auto_start:
        return
    start_order = topology.get("startOrder", [])
    started_ids = _start_ordered_containers(
        host, project_id, containers, start_order, topology, auto_start
    )
    for ctr in containers:
        if ctr["node_id"] in started_ids:
            continue
        if ctr.get("is_pod"):
            full_pod_name = f"troshka-{project_id[:8]}-{ctr['name']}"
            timeout = 900 if ctr.get("build_content", True) else 120
            _start_pod(host, full_pod_name, timeout=timeout)
        else:
            container_name = f"troshka-{project_id[:8]}-{ctr['node_id'][:8]}"
            _start_container(host, container_name)
        for node in topology.get("nodes", []):
            if node["id"] == ctr["node_id"]:
                node.setdefault("data", {})["status"] = "running"
                break


def _deploy_start_vms(s, host, project_id, project, topology, auto_start):
    if not auto_start:
        return True
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
        logger.error(_LOG_DEPLOY, project_id[:8], error_msg)
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
        return False
    return True


def _deploy_finalize_timers(project, auto_start):
    _ = auto_start
    if project.state == "active" and project.auto_stop_minutes:
        now = datetime.datetime.now(datetime.UTC)
        project.auto_stop_started_at = now
        project.auto_stop_expires_at = now + datetime.timedelta(
            minutes=project.auto_stop_minutes
        )
        project.auto_stop_warned = False
    if project.auto_delete_minutes and not project.auto_delete_started_at:
        now = datetime.datetime.now(datetime.UTC)
        project.auto_delete_started_at = now
        project.lifetime_expires_at = now + datetime.timedelta(
            minutes=project.auto_delete_minutes
        )
        project.auto_delete_warned = False


def _deploy_create_dns_records(
    s, project_id, project, topology, lb_config, external_ips
):
    _ = topology
    if not (project.dns_provider_id and project.guid and project.domain):
        return
    from app.models.dns_provider import DnsProvider
    from app.services.dns_service import create_dns_records, resolve_dns_records

    dns_provider = s.query(DnsProvider).filter_by(id=project.dns_provider_id).first()
    if not dns_provider or not lb_config:
        return
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
    if not dns_templates:
        return
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
    deployed_topo["_dns_records"] = [r for r in records if r.get("value")]
    project.deployed_topology = deployed_topo
    if errors:
        logger.warning(
            "Deploy %s: DNS record creation had errors: %s",
            project_id[:8],
            errors,
        )


def _deploy_store_bmc_topology(project, topology, bmc_config):
    if not bmc_config:
        return
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


def _cleanup_stale_shared_cache(s, project):
    if not project.host_id:
        return
    h = s.query(Host).filter_by(id=project.host_id).first()
    if not h:
        return
    pool = _get_host_pool(h, s)
    if not pool or not pool.mode.startswith("shared"):
        return
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


# ── Deploy pipeline helpers ───────────────────────────────────────────────


def _set_deploy_error(s, project, error_msg):
    """Set project to error state without notification."""
    project.state = "error"
    project.deploy_error = error_msg
    s.commit()


def _set_deploy_error_and_notify(s, project_id, project, error_msg):
    """Set project to error state and send WS notification."""
    project.state = "error"
    project.deploy_error = error_msg
    s.commit()
    notify_project(
        project_id,
        {"type": "project-state", "state": "error", "deploy_error": error_msg},
    )


def _deploy_resolve_host(s, project, project_id):
    """Resolve or auto-place a host for the project.

    Returns ``(host, error_msg)`` where *error_msg* is ``None`` on success.
    """
    from app.models.host import Host

    host = (
        s.query(Host).filter_by(id=project.host_id).first() if project.host_id else None
    )
    if not host and not project.host_id:
        from app.services.pattern_locations import pattern_disk_ids_from_topology
        from app.services.placement import (
            calculate_project_requirements,
            find_available_host,
        )

        reqs = calculate_project_requirements(project.topology or {})
        pattern_disk_ids = pattern_disk_ids_from_topology(project.topology or {})
        host = find_available_host(
            s,
            reqs["total_vcpus"],
            reqs["total_ram_mb"],
            pattern_disk_ids=pattern_disk_ids,
        )
        if host:
            project.host_id = host.id
            s.commit()
            logger.info(
                "Deploy %s: auto-placed on host %s", project_id[:8], host.id[:8]
            )
    if not host or not host.ip_address:
        return host, _deploy_host_error_msg(s, project, host)
    return host, None


def _deploy_host_error_msg(s, project, host):
    """Build a human-readable error when host resolution fails.

    When auto-placement found no host, delegate to the placement diagnostic so
    the message names the real cause — CPU, RAM, or pattern-disk availability —
    rather than a blanket 'not enough capacity'.
    """
    if not project.host_id:
        from app.services.pattern_locations import pattern_disk_ids_from_topology
        from app.services.placement import (
            calculate_project_requirements,
            diagnose_placement_failure,
        )

        reqs = calculate_project_requirements(project.topology or {})
        pattern_disk_ids = pattern_disk_ids_from_topology(project.topology or {})
        return diagnose_placement_failure(
            s,
            reqs["total_vcpus"],
            reqs["total_ram_mb"],
            pattern_disk_ids=pattern_disk_ids,
        )
    if not host:
        return "Assigned host no longer exists"
    return "Assigned host has no IP address — it may still be provisioning"


def _deploy_init_context(s, project, project_id):
    """Compute clock offset and allocate VNIs.

    Returns ``(topology, clock_offset, vni_map)``.
    """
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
    from app.services.deploy_topology import inject_showroom_gateway_port_forwards

    provider_type = None
    if project.host_id:
        from app.models.host import Host
        from app.models.provider import Provider

        host = s.query(Host).filter_by(id=project.host_id).first()
        if host and host.provider_id:
            prov = s.query(Provider).filter_by(id=host.provider_id).first()
            provider_type = prov.type if prov else None
    if inject_showroom_gateway_port_forwards(topology, vni_map, provider_type):
        project.topology = topology
        s.commit()
        logger.info(
            "Deploy %s: injected showroom gateway port forwards", project_id[:8]
        )
    return topology, clock_offset, vni_map


def _deploy_disable_guest_exec(project, topology):
    """Mark VMs with cloud-init as guest-exec disabled when project flag is off."""
    if not project.guest_exec_enabled:
        for node in topology.get("nodes", []):
            if node.get("type") == "vmNode" and node.get("data", {}).get("cloudInit"):
                node["data"]["guestExecEnabled"] = False


def _deploy_cache_images_and_pxe(host, project_id, topology, vni_map, s, project=None):
    """Download library images and set up PXE boot services."""
    _checkpoint(s, project_id, "images")
    _update_deploy_progress(project_id, "downloading images", "0%")
    logger.info("Deploy %s: caching library images", project_id[:8])

    def _progress(detail, items):
        _update_deploy_progress(
            project_id, "downloading images", str(detail), items=items
        )

    _prepare_topology_library_refs(topology, s, project)
    cache_library_images(topology, host, s, progress_callback=_progress)
    logger.info("Deploy %s: setting up PXE boot services", project_id[:8])
    _setup_pxe_via_troshkad(host, topology, vni_map, project_id)


def _deploy_create_bmc_bridge(host, project_id, topology):
    """Create BMC bridge on host if topology contains a BMC network."""
    bmc_config = _extract_bmc_config(topology, project_id)
    if bmc_config:
        net_data = bmc_config["bmc_network"]
        cidr = net_data.get("cidr", "192.168.100.0/24")
        _bj = start_job(
            host,
            "/bmc/create-bridge",
            {
                "project_id": project_id,
                "bmc_cidr": cidr,
                "bmc_gateway_ip": cidr.rsplit(".", 1)[0] + ".1",
                "vms": [{"bmc_ip": vm["bmc_ip"]} for vm in bmc_config["vms"]],
            },
        )
        wait_for_job(host, _bj, timeout=30)
        logger.info("Deploy %s: BMC bridge created", project_id[:8])


def _deploy_single_host_setup(
    s, project, host, topology, vni_map, project_id, resume_from, pool
):
    """Provision networks, seeds, images, and BMC bridge for single-host deploy.

    Returns a dict with ``lb_config`` and ``external_ips`` on success, or
    ``None`` if the deploy should stop (error state already set or project
    was deleted mid-deploy).
    """
    external_ips = topology.get("externalIps", [])
    if external_ips and not _should_skip(resume_from, "eips"):
        err = _deploy_allocate_eips(
            s, project_id, project, host, topology, external_ips
        )
        if err:
            _set_deploy_error(s, project, err)
            _delete_deploy_progress(project_id)
            return None

    _auto_assign_container_ips(topology)

    lb_config = None
    if not _should_skip(resume_from, "networks"):
        _checkpoint(s, project_id, "networks")
        _update_deploy_progress(project_id, "networking", "waiting for lock")
        with _get_network_lock(host.id):
            _update_deploy_progress(project_id, "networking", "configuring VXLAN")
            logger.info(
                "Deploy %s: setting up networks on %s",
                project_id[:8],
                host.ip_address,
            )
            net_result = _setup_networks_via_troshkad(
                host, topology, vni_map, s, project_id
            )
        if net_result is not True:
            logger.error(_LOG_DEPLOY, project_id[:8], net_result)
            _set_deploy_error(s, project, net_result)
            _delete_deploy_progress(project_id)
            return None

        lb_config = _deploy_setup_lb(host, project_id, topology, vni_map)

        if external_ips:
            _deploy_sync_sg_rules(s, project_id, project, host, topology, lb_config)

        if _project_deleted(project_id):
            _delete_deploy_progress(project_id)
            return None

        _deploy_inject_gateway_ip(topology, project_id)
        _deploy_disable_guest_exec(project, topology)
        _deploy_create_provider_routes(s, project_id, topology, host=host)

    if not _should_skip(resume_from, "seeds"):
        _checkpoint(s, project_id, "seeds")
        _update_deploy_progress(project_id, "cloud-init", "creating seed ISOs")
        logger.info("Deploy %s: creating cloud-init seed ISOs", project_id[:8])
        _create_seed_isos_via_troshkad(host, project_id, topology, pool)

        _update_deploy_progress(project_id, "cloud-init", "deploying metadata service")
        logger.info("Deploy %s: deploying metadata service", project_id[:8])
        _setup_metadata_via_troshkad(host, project_id, topology, vni_map)

        if _project_deleted(project_id):
            _delete_deploy_progress(project_id)
            return None

    if not _should_skip(resume_from, "images"):
        _deploy_cache_images_and_pxe(host, project_id, topology, vni_map, s, project)

    if not _should_skip(resume_from, "container_pull"):
        _checkpoint(s, project_id, "container_pull")
        _deploy_pull_container_images(host, project_id, topology, s)

    if _project_deleted(project_id):
        _delete_deploy_progress(project_id)
        return None

    bmc_err = _deploy_validate_bmc(project_id, topology)
    if bmc_err:
        logger.error(_LOG_DEPLOY, project_id[:8], bmc_err)
        _set_deploy_error_and_notify(s, project_id, project, bmc_err)
        _delete_deploy_progress(project_id)
        return None

    _deploy_create_bmc_bridge(host, project_id, topology)

    if _project_deleted(project_id):
        _delete_deploy_progress(project_id)
        return None

    return {"lb_config": lb_config, "external_ips": external_ips}


def _deploy_single_host_execute(
    s,
    host,
    project_id,
    project,
    topology,
    vni_map,
    pool,
    disk_cache,
    clock_offset,
    auto_start,
    lb_config,
    external_ips,
    resume_from: str | None = None,
):
    """Create VMs, start them, and finalize single-host deploy."""
    vms = _extract_vms(topology)
    bmc_config = None

    if not _should_skip(resume_from, "disks"):
        _checkpoint(s, project_id, "disks")
        _update_deploy_progress(project_id, "creating", "VMs")
        logger.info("Deploy %s: creating VMs", project_id[:8])
        vms = _deploy_create_disks(host, project_id, topology, pool)
        _deploy_handle_recert(s, host, project_id, topology, pool)

    if not _should_skip(resume_from, "vms"):
        _checkpoint(s, project_id, "vms")
        _deploy_define_vms(
            host, project_id, vms, topology, vni_map, pool, disk_cache, clock_offset
        )

        project.topology = topology
        s.commit()

        bmc_err, bmc_config = _deploy_setup_bmc(host, project_id, topology)
        if bmc_err:
            _set_deploy_error(s, project, bmc_err)
            _delete_deploy_progress(project_id)
            return

    if not _should_skip(resume_from, "containers"):
        _checkpoint(s, project_id, "containers")
        _deploy_create_containers(host, project_id, topology, vni_map, pool)

    if _project_deleted(project_id):
        _delete_deploy_progress(project_id)
        return

    if not _should_skip(resume_from, "starting"):
        _checkpoint(s, project_id, "starting")
        if not _deploy_start_vms(s, host, project_id, project, topology, auto_start):
            return

        _deploy_start_containers(host, project_id, topology, auto_start)
        project.topology = topology
        s.commit()

        if _should_use_ops_pod(topology):
            _deploy_ops_pod(s, host, project_id, project, topology, vni_map)

    _deploy_complete_and_notify(
        s,
        project_id,
        project,
        topology,
        vms,
        lb_config,
        external_ips,
        auto_start,
        bmc_config,
    )


def _deploy_complete_and_notify(
    s,
    project_id,
    project,
    topology,
    vms,
    lb_config,
    external_ips,
    auto_start,
    bmc_config,
):
    """Set final project state and send success notifications."""
    project.state = "active" if auto_start else "stopped"
    project.deploy_error = None
    project.deploy_step = None
    project.deploy_progress = None
    project.deployed_topology = project.topology

    _deploy_finalize_timers(project, auto_start)
    _deploy_create_dns_records(
        s, project_id, project, topology, lb_config, external_ips
    )
    _deploy_store_bmc_topology(project, topology, bmc_config)

    s.commit()
    _notify_client_topology_update(project_id, project, s)
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
    if external_ips:
        notify_project(
            project_id,
            {"type": "external-ips-updated", "externalIps": external_ips},
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


def _deploy_handle_failure(s, project_id, exception):
    """Handle unexpected exception during deploy."""
    logger.exception("Deploy %s failed unexpectedly", project_id[:8])
    _delete_deploy_progress(project_id)
    try:
        from app.models.project import Project

        project = s.query(Project).filter_by(id=project_id).first()
        if project:
            project.state = "error"
            project.deploy_error = str(exception)
            _cleanup_stale_shared_cache(s, project)
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


# ── Main deploy orchestrator ─────────────────────────────────────────────


def _deploy_project_inner(  # pyright: ignore[reportGeneralTypeIssues]
    project_id: str, auto_start: bool = True, resume_from: str | None = None
):
    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.services.placement import record_deploy_end, record_deploy_start

    _clear_deploy_cancelled(project_id)

    _host_id_for_inflight: str | None = None
    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "deploying":
            return
        if project.deploy_error:
            project.deploy_error = None
            s.commit()
        if resume_from:
            logger.info(
                "Deploy %s: resuming from step '%s'", project_id[:8], resume_from
            )

        host, host_err = _deploy_resolve_host(s, project, project_id)
        if host:
            _host_id_for_inflight = host.id
            record_deploy_start(host.id)
        if host_err:
            _set_deploy_error_and_notify(s, project_id, project, host_err)
            return
        assert host is not None  # guaranteed when host_err is None

        topology, clock_offset, vni_map = _deploy_init_context(s, project, project_id)

        # Multi-host deploy: mesh setup -> network setup -> VM distribution
        if project.mesh_network_host_id:
            logger.info(
                "Deploy %s: multi-host mode (network host: %s)",
                project_id[:8],
                project.mesh_network_host_id[:8],
            )
            _deploy_multihost(project_id, project, s)
            return

        # KubeVirt native: delegate entire deploy to operator via CRDs
        if host.host_type == "kubevirt-cluster":
            _deploy_kubevirt_native(project_id, project, host, topology, s)
            return

        pool = _get_host_pool(host, s)
        disk_cache = "none" if pool and pool.mode.startswith("shared") else None

        ctx = _deploy_single_host_setup(
            s, project, host, topology, vni_map, project_id, resume_from, pool
        )
        if ctx is None:
            return

        _deploy_single_host_execute(
            s,
            host,
            project_id,
            project,
            topology,
            vni_map,
            pool,
            disk_cache,
            clock_offset,
            auto_start,
            ctx["lb_config"],
            ctx["external_ips"],
            resume_from,
        )
    except Exception as e:
        _deploy_handle_failure(s, project_id, e)
    finally:
        if _host_id_for_inflight:
            record_deploy_end(_host_id_for_inflight)
        s.close()


def _find_bastion_disk_path(vms, topology, project_id, pool):
    """Find the bastion VM's boot disk path, or None if no bastion."""
    bastion_vm = next((v for v in vms if v.get("name") == "bastion"), None)
    if not bastion_vm:
        return None
    bastion_disks = _find_vm_disks(bastion_vm["node_id"], topology)
    bastion_boot = next((d for d in bastion_disks if d.get("format") == "qcow2"), None)
    if not bastion_boot:
        return None
    return _disk_path(
        project_id,
        bastion_vm["node_id"],
        bastion_boot["node_id"],
        bastion_boot["format"],
        pool,
    )


def _build_recert_params(vm, disk, bastion_disk_path, project_id, common_password):
    """Build recert job parameters and resolve kubeadmin password."""
    vm_name = vm.get("name", vm["node_id"][:8])
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
    return recert_params, kubeadmin_pw


def _apply_recert_results(topology, vm_node_id, common_password, kubeadmin_pw, kc):
    """Update topology node data with recert results (kubeconfig, password)."""
    for n in topology.get("nodes", []):
        if n["id"] == vm_node_id:
            if common_password:
                n.setdefault("data", {})["ocpKubeadminPassword"] = kubeadmin_pw
            if kc:
                n.setdefault("data", {})["ocpKubeconfig"] = kc
            break


def _handle_recert_failure(pattern_recert, project_id, vm_name, err):
    """Handle a failed recert job. Raises RuntimeError if recert was required."""
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


def _run_recert_for_vm(
    host,
    project_id,
    vm,
    topology,
    pool,
    bastion_disk_path,
    pattern_recert,
    common_password,
):
    """Run recert on a single VM. Returns True if recert succeeded."""
    vm_disks = _find_vm_disks(vm["node_id"], topology)
    boot_disk = next((d for d in vm_disks if d.get("format") == "qcow2"), None)
    if not boot_disk:
        return False
    disk = _disk_path(
        project_id,
        vm["node_id"],
        boot_disk["node_id"],
        boot_disk["format"],
        pool,
    )
    vm_name = vm.get("name", vm["node_id"][:8])
    recert_params, kubeadmin_pw = _build_recert_params(
        vm,
        disk,
        bastion_disk_path,
        project_id,
        common_password,
    )
    try:
        job_id = start_job(host, "/vms/recert", recert_params)
        job = wait_for_job(host, job_id, timeout=300)
        if job.get("status") == "completed":
            logger.info("Deploy %s: recert completed for %s", project_id[:8], vm_name)
            kc = job.get("result", {}).get("kubeconfig")
            _apply_recert_results(
                topology, vm["node_id"], common_password, kubeadmin_pw, kc
            )
            return True
        err = job.get("result", {}).get("error", "unknown")
        _handle_recert_failure(pattern_recert, project_id, vm_name, err)
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
    return False


def _guestfish_clean_kubelet_certs(host, project_id, vm, topology, pool):
    """Delete stale kubelet PKI from a single RHCOS VM disk via guestfish."""
    vm_disks = _find_vm_disks(vm["node_id"], topology)
    boot_disk = next(
        (d for d in vm_disks if d.get("format") == "qcow2"),
        None,
    )
    vm_name = vm.get("name", vm["node_id"][:8])
    if not boot_disk:
        logger.warning(
            "Deploy %s: no qcow2 boot disk for RHCOS VM %s, skipping cert cleanup",
            project_id[:8],
            vm_name,
        )
        return

    disk = _disk_path(
        project_id,
        vm["node_id"],
        boot_disk["node_id"],
        boot_disk["format"],
        pool,
    )
    operations = [
        {"action": "rm-rf", "path": "/var/lib/kubelet/pki"},
        {"action": "rm-f", "path": "/var/lib/kubelet/kubeconfig"},
    ]
    logger.info("Deploy %s: cleaning kubelet certs from %s", project_id[:8], vm_name)
    try:
        job_id = start_job(
            host,
            "/vms/modify-fs",
            {"disk": disk, "operations": operations},
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
                "Deploy %s: cert cleanup complete for %s",
                project_id[:8],
                vm_name,
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
    bastion_disk_path = _find_bastion_disk_path(vms, topology, project_id, pool)

    recert_succeeded = set()
    for i, vm in enumerate(recert_vms):
        logger.info(
            "Deploy %s: running recert on disk for %s (%d/%d)",
            project_id[:8],
            vm.get("name", vm["node_id"][:8]),
            i + 1,
            len(recert_vms),
        )
        if _run_recert_for_vm(
            host,
            project_id,
            vm,
            topology,
            pool,
            bastion_disk_path,
            pattern_recert,
            common_password,
        ):
            recert_succeeded.add(vm["node_id"])

    if recert_succeeded and len(recert_succeeded) == len(recert_vms):
        return

    for vm in rhcos_vms:
        if vm["node_id"] not in recert_succeeded:
            _guestfish_clean_kubelet_certs(host, project_id, vm, topology, pool)


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


def _exec_on_bastion_kubevirt(
    host, project_id, bastion_ip, _password, command, timeout
):
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


def _verify_bastion_browser(
    exec_fn, push_fn, project_id, vm_name=None, status_phase="browser"
):
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
        "LJ=$(find /home/cloud-user/.mozilla/firefox -name logins.json 2>/dev/null | head -1); "
        "PW_FILE=/home/cloud-user/ocp-install/auth/kubeadmin-password; "
        'if [ -n "$LJ" ] && grep -q encryptedUsername "$LJ" 2>/dev/null; then '
        '  if [ -f "$PW_FILE" ]; then '
        '    PW_MT=$(stat -c %Y "$PW_FILE" 2>/dev/null || echo 0); '
        '    LJ_MT=$(stat -c %Y "$LJ" 2>/dev/null || echo 0); '
        '    if [ "$LJ_MT" -ge "$PW_MT" ]; then echo "logins:ok"; else echo "logins:stale"; fi; '
        '  else echo "logins:ok"; fi; '
        'elif [ -z "$LJ" ]; then echo "logins:missing"; '
        'else echo "logins:stale"; fi'
    )
    _CA_UPDATE_CMD = (
        "oc get secret -n openshift-ingress router-certs-default "
        "-o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d "
        "| sudo tee /etc/pki/ca-trust/source/anchors/ocp-ingress.pem >/dev/null "
        "&& sudo update-ca-trust"
    )
    _AUTOLOGIN_CMD = (
        "export GECKODRIVER_PATH=/usr/local/bin/geckodriver; "
        "CONSOLE_URL=$(oc whoami --show-console 2>/dev/null); "
        '[ -n "$CONSOLE_URL" ] && [ -f /home/cloud-user/ocp-autologin.py ] && '
        'python3 /home/cloud-user/ocp-autologin.py "$CONSOLE_URL" 2>&1 || true'
    )

    for _ in range(18):
        verify = exec_fn(_VERIFY_SCRIPT, timeout=20)
        if verify and "ca:ok" in verify and "logins:ok" in verify:
            return True

        if _apply_bastion_browser_fixes(
            verify, exec_fn, push_fn, _CA_UPDATE_CMD, _AUTOLOGIN_CMD, status_phase
        ):
            _t.sleep(5)
            continue
        push_fn(status_phase, "waiting for bastion setup")
        _t.sleep(10)

    logger.warning("OCP monitor %s: bastion browser setup incomplete", label)
    return False


def _apply_bastion_browser_fixes(
    verify, exec_fn, push_fn, ca_cmd, autologin_cmd, status_phase="browser"
):
    """Check for and apply CA cert / browser credential fixes. Returns True if fixes applied."""
    needs_fix = []
    if verify and "ca:stale" in verify:
        needs_fix.append(_MSG_CA_CERT)
    if verify and ("logins:stale" in verify or "logins:missing" in verify):
        needs_fix.append(_MSG_BROWSER_CREDS)
    if not needs_fix:
        return False
    push_fn(status_phase, f"bastion {', '.join(needs_fix)} stale, updating...")
    if _MSG_CA_CERT in needs_fix:
        exec_fn(ca_cmd, timeout=15)
    if _MSG_BROWSER_CREDS in needs_fix:
        exec_fn(_KILL_BROWSER_CMD, timeout=10)
        exec_fn(_CLEAR_BASTION_OCP_COOKIES_CMD, timeout=10)
        exec_fn(_ENSURE_FIREFOX_PROFILE_CMD, timeout=20)
        exec_fn(autologin_cmd, timeout=90)
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


def _node_kubeadmin_password(node):
    return node.get("data", {}).get("ocpKubeadminPassword")


def _first_kubeadmin_password(nodes, predicate):
    return next(
        (
            pw
            for n in nodes
            if predicate(n)
            for pw in [_node_kubeadmin_password(n)]
            if pw
        ),
        None,
    )


def _extract_ocp_kubeadmin_password(nodes, vm_id=None):
    """Return kubeadmin password from topology (set after recert)."""
    if vm_id:
        pw = _first_kubeadmin_password(nodes, lambda n: n.get("id") == vm_id)
        if pw:
            return pw
    return _first_kubeadmin_password(nodes, lambda n: n.get("type") == "vmNode")


def _persist_ocp_kubeadmin_password(project_id, vm_id, pw):
    """Store the installer-generated kubeadmin password on the VM node so the UI
    shows the real value. Recert sets ocpKubeadminPassword directly; the Agent
    Installer generates it on the bastion, so the monitor reads it back here."""
    try:
        from sqlalchemy.orm.attributes import flag_modified

        from app.core.database import SessionLocal
        from app.models.project import Project

        db = SessionLocal()
        try:
            p = db.query(Project).filter_by(id=project_id).first()
            if not p:
                return
            changed = False
            for attr in ("deployed_topology", "topology"):
                topo = getattr(p, attr, None)
                if not topo:
                    continue
                for n in topo.get("nodes", []):
                    if n.get("id") == vm_id and n.get("type") == "vmNode":
                        data = n.setdefault("data", {})
                        if data.get("ocpKubeadminPassword") != pw:
                            data["ocpKubeadminPassword"] = pw
                            flag_modified(p, attr)
                            changed = True
            if changed:
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to persist kubeadmin password for %s", project_id[:8])


def _write_bastion_kubeadmin_password(
    host, project_id, bastion_ip, ssh_password, kubeadmin_pw
):
    """Write kubeadmin password to the bastion auth directory."""
    import base64

    pw_b64 = base64.b64encode(kubeadmin_pw.encode()).decode()
    _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        ssh_password,
        "mkdir -p /home/cloud-user/ocp-install/auth && "
        f"echo '{pw_b64}' | base64 -d > /home/cloud-user/ocp-install/auth/kubeadmin-password && "
        "chown cloud-user:cloud-user /home/cloud-user/ocp-install/auth/kubeadmin-password",
        timeout=10,
    )


def _ensure_bastion_geckodriver(host, project_id, bastion_ip, ssh_password):
    """Install geckodriver on bastion if missing (bastion-builder normally includes it)."""
    from app.services.ocp_autologin import GECKODRIVER_URL

    _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        ssh_password,
        "GECKO=/usr/local/bin/geckodriver; "
        'if [ ! -x "$GECKO" ]; then '
        f"curl -sfL {GECKODRIVER_URL} | sudo tar xz -C /usr/local/bin/; "
        "sudo chmod +x /usr/local/bin/geckodriver; fi; "
        "test -x /usr/local/bin/geckodriver && echo geckodriver:ok || echo geckodriver:missing",
        timeout=60,
    )


def _ensure_bastion_selenium(host, project_id, bastion_ip, ssh_password):
    """Ensure selenium Python package is available on the bastion."""
    _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        ssh_password,
        "python3 -c 'import selenium' 2>/dev/null || pip3 install --user selenium",
        timeout=120,
    )


def _deploy_bastion_autologin_script(host, project_id, bastion_ip, ssh_password):
    """Deploy Selenium-based ocp-autologin.py (replaces bastion-image variant)."""
    import base64

    from app.services.ocp_autologin import OCP_AUTOLOGIN_SCRIPT

    script_b64 = base64.b64encode(OCP_AUTOLOGIN_SCRIPT.encode()).decode()
    _exec_on_bastion(
        host,
        project_id,
        bastion_ip,
        ssh_password,
        f"echo '{script_b64}' | base64 -d > /home/cloud-user/ocp-autologin.py && "
        "chown cloud-user:cloud-user /home/cloud-user/ocp-autologin.py && "
        "chmod 755 /home/cloud-user/ocp-autologin.py",
        timeout=10,
    )


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
    normalized = (code or "").strip()
    if not normalized or set(normalized) <= {"0"}:
        return ""
    return f" (HTTP {normalized})"


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
    status_phase="browser",
):
    """Configure bastion browser if flag is set, then clean up temp kubeconfig."""
    vm_node = next((n for n in nodes if n["id"] == vm_id), None)
    configure_browser = vm_node and vm_node.get("data", {}).get(
        "configureBastionBrowser"
    )
    if configure_browser:
        # Copy kubeconfig to bastion default locations (skip if already using bastion default)
        if kc_path:
            _push(status_phase, "setting bastion kubeconfig for this cluster")
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
        _push(status_phase, "refreshing bastion CA trust")
        _oc(
            "oc get secret -n openshift-ingress router-certs-default "
            "-o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d "
            "| sudo tee /etc/pki/ca-trust/source/anchors/ocp-ingress.pem >/dev/null "
            "&& sudo update-ca-trust",
            timeout=15,
        )

        kubeadmin_pw = _extract_ocp_kubeadmin_password(nodes, vm_id)
        if kubeadmin_pw:
            _push(status_phase, "syncing kubeadmin password to bastion")
            _write_bastion_kubeadmin_password(
                host, project_id, bastion_ip, password, kubeadmin_pw
            )
        elif vm_id:
            # Agent Installer generated the kubeadmin password on the bastion;
            # read it back so the UI shows the real value (not the bastion pw).
            read_pw = _oc(
                "cat /home/cloud-user/ocp-install/auth/kubeadmin-password "
                "2>/dev/null",
                timeout=10,
            )
            read_pw = (read_pw or "").strip()
            if read_pw:
                _push(status_phase, "reading kubeadmin password from bastion")
                _persist_ocp_kubeadmin_password(project_id, vm_id, read_pw)

        _push(status_phase, "deploying browser autologin script")
        _ensure_bastion_geckodriver(host, project_id, bastion_ip, password)
        _ensure_bastion_selenium(host, project_id, bastion_ip, password)
        _deploy_bastion_autologin_script(host, project_id, bastion_ip, password)

        _push(status_phase, "ensuring Firefox profile")
        _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            _KILL_BROWSER_CMD
            + "; "
            + _CLEAR_BASTION_OCP_COOKIES_CMD
            + "; "
            + _ENSURE_FIREFOX_PROFILE_CMD,
            timeout=25,
        )

        # Verify CA fingerprint + run autologin with retry loop
        _push(status_phase, "verifying bastion browser setup")
        bastion_ready = _verify_bastion_browser(
            _oc, _push, project_id, vm_name, status_phase=status_phase
        )
        return bastion_ready

    # Cleanup temp kubeconfig (skip if we used bastion default or copied it there)
    if kc_path and not configure_browser:
        _exec_on_bastion(
            host, project_id, bastion_ip, password, f"rm -f {kc_path}", timeout=5
        )
    return None


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


def _ocp_vm_restart_ingress(_oc, _push, skip=False):
    if skip:
        return
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


def _ocp_vm_monitor_load_context(db, project_id, host_id, vm_name):
    """Load host/project for OCP VM monitor; return (host, topo) or None."""
    from app.models.host import Host as _Host3
    from app.models.project import Project as _VmProj

    host = db.query(_Host3).filter_by(id=host_id).first()
    if not host:
        logger.warning(
            "OCP VM monitor %s/%s: host %s not found",
            project_id[:8],
            vm_name,
            host_id[:8],
        )
        return None

    project = db.query(_VmProj).filter_by(id=project_id).first()
    if not project or project.state != "active":
        logger.warning(
            "OCP VM monitor %s/%s: project not found or not active (state=%s)",
            project_id[:8],
            vm_name,
            project.state if project else "missing",
        )
        return None

    topo = project.deployed_topology or project.topology or {}
    return host, topo


def _ocp_vm_monitor_finalize(
    project_id, vm_name, configure_browser, browser_ready, elapsed_secs, _push
):
    """Push final monitor status and persist ocp_status."""
    if configure_browser and not browser_ready:
        _push("warning", f"{vm_name} cluster ready (bastion browser not configured)")
        _ocp_update_status(project_id, "warning", elapsed_secs)
        return

    _push("ready", f"{vm_name} cluster ready")
    _ocp_update_status(project_id, "ready", elapsed_secs)


def _ocp_vm_health_inner(
    project_id, host_id, vm_id, vm_name, kubeconfig_content, deploy_start, db
):
    import time as _t

    logger.info(
        "OCP VM monitor %s/%s: inner started (host=%s)",
        project_id[:8],
        vm_name,
        host_id[:8],
    )
    loaded = _ocp_vm_monitor_load_context(db, project_id, host_id, vm_name)
    if not loaded:
        return
    host, topo = loaded

    start = deploy_start or _t.time()

    def _elapsed():
        s = int(_t.time() - start)
        return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"

    def _push(phase, detail, items=None):
        detail_with_time = f"{detail} ({_elapsed()})"
        _ocp_push_status(
            project_id,
            phase,
            detail_with_time,
            items,
            vm_id=vm_id,
            vm_name=vm_name,
        )

    # Find bastion for exec
    nodes = topo.get("nodes", [])
    vm_node = next((n for n in nodes if n["id"] == vm_id), None)
    is_recert = bool(vm_node and vm_node.get("data", {}).get("recertEnabled"))
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
        _ocp_update_status(project_id, "error", int(_t.time() - start))
        return

    _ocp_vm_poll_with_csrs(_oc, _approve_csrs, _push, deadline)
    _ocp_vm_restart_ingress(_oc, _push, skip=is_recert)
    if not is_recert:
        _t.sleep(10)
    _ocp_vm_wait_for_console(_oc, _approve_csrs, _push, deadline)

    _ocp_vm_final_csr_sweep(_approve_csrs, _push)

    configure_browser = bool(
        vm_node and vm_node.get("data", {}).get("configureBastionBrowser")
    )
    browser_ready = True
    if configure_browser:
        browser_ready = bool(
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
                status_phase="browser",
            )
        )

    elapsed_secs = int(_t.time() - start)
    _ocp_vm_monitor_finalize(
        project_id, vm_name, configure_browser, browser_ready, elapsed_secs, _push
    )

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
            "waiting for console route" + _http_suffix(http_code),
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
            "waiting for OAuth route" + _http_suffix(oauth_code),
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


def _topology_pattern_used_recert(topology):
    """Return True if any storageNode pattern has recert enabled."""
    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        pid = node.get("data", {}).get("patternId")
        if not pid:
            continue
        try:
            from app.core.database import SessionLocal as _SL

            _db = _SL()
            pat = _db.query(Pattern).filter_by(id=pid).first()
            used_recert = bool(pat and pat.recert)
            _db.close()
            return used_recert
        except Exception:
            return False
    return False


def _topology_rhcos_vm_count(topology):
    return sum(
        1
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode" and n.get("data", {}).get("os") == "rhcos"
    )


def _ocp_post_pattern_cert_refresh(
    host, project_id, bastion_ip, password, topology, push_fn
):
    """Post-pattern deploy: refresh bastion certs if recert was used."""
    used_recert = _topology_pattern_used_recert(topology)
    rhcos_count = _topology_rhcos_vm_count(topology)
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

    kubeadmin_pw = _extract_ocp_kubeadmin_password(topology.get("nodes", []))
    if kubeadmin_pw:
        push_fn("certs", "syncing kubeadmin password to bastion")
        _write_bastion_kubeadmin_password(
            host, project_id, bastion_ip, password, kubeadmin_pw
        )

    _deploy_bastion_autologin_script(host, project_id, bastion_ip, password)
    _ensure_bastion_geckodriver(host, project_id, bastion_ip, password)
    _ensure_bastion_selenium(host, project_id, bastion_ip, password)

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


def _ocp_push_status(project_id, phase, detail, items=None, vm_id=None, vm_name=None):
    """Push OCP health status via WebSocket and persist to DB."""
    msg = {"type": "ocp-health", "phase": phase, "detail": detail}
    if items:
        msg["items"] = items
    if vm_id:
        msg["vm_id"] = vm_id
    if vm_name:
        msg["vm_name"] = vm_name
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


def _stop_kubevirt_vms(s, host, project_id, vms):
    """Patch KubeVirt VMs to Halted via K8s API."""
    from app.models.provider import Provider
    from app.services.providers.kubevirt import (
        _get_k8s_clients,
        _project_ns,
        patch_kubevirt_run_strategy,
    )

    provider = s.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return
    custom_api, _, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    for vm in vms:
        kv_name = f"troshka-vm-{vm['node_id'][:8]}"
        try:
            patch_kubevirt_run_strategy(custom_api, namespace, kv_name, "Halted")
        except Exception as e:
            logger.warning(
                "Stop %s: failed to stop KubeVirt VM %s: %s",
                project_id[:8],
                kv_name,
                e,
            )


def _stop_troshkad_vms(host, project_id, vms):
    """Stop VMs via troshkad."""
    for vm in vms:
        vm_name = _vm_domain_name(project_id, vm["node_id"])
        try:
            job_id = start_job(host, "/vms/stop", {"domain_name": vm_name})
            wait_for_job(host, job_id, timeout=90)
        except TroshkadError as e:
            logger.warning(
                "Stop %s: failed to stop %s: %s",
                project_id[:8],
                vm_name,
                e,
            )


def _set_project_error(s, project_id, error_msg, project=None):
    """Set project to error state and notify.

    If *project* is passed, uses it directly; otherwise queries the session.
    """
    if project is None:
        from app.models.project import Project

        project = s.query(Project).filter_by(id=project_id).first()
    if project:
        project.state = "error"
        project.deploy_error = error_msg
        s.commit()
        notify_project(
            project_id,
            {"type": "project-state", "state": "error", "deploy_error": error_msg},
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
            _set_project_error(
                s,
                project_id,
                "Host is disconnected or unavailable — cannot stop VMs",
                project=project,
            )
            return

        topology = project.topology or {}
        vms = _extract_vms(topology)

        if host.host_type == "kubevirt-cluster":
            _stop_kubevirt_vms(s, host, project_id, vms)
        elif not host.ip_address:
            _set_project_error(
                s,
                project_id,
                "Host is disconnected or unavailable — cannot stop VMs",
                project=project,
            )
            return
        else:
            _stop_troshkad_vms(host, project_id, vms)

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
            _set_project_error(
                s,
                project_id,
                "Stop failed unexpectedly. Check server logs.",
            )
        except Exception:
            pass
    finally:
        s.close()


def _start_kubevirt_vms(s, host, project_id, vms):
    """Start KubeVirt VMs via runStrategy (not deprecated spec.running)."""
    from app.models.provider import Provider
    from app.services.providers.kubevirt import (
        _get_k8s_clients,
        _project_ns,
        patch_kubevirt_run_strategy,
    )

    provider = s.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return
    custom_api, _, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    for vm in vms:
        kv_name = f"troshka-vm-{vm['node_id'][:8]}"
        try:
            patch_kubevirt_run_strategy(custom_api, namespace, kv_name, "Always")
        except Exception as e:
            logger.warning(
                "Start %s: failed to start KubeVirt VM %s: %s",
                project_id[:8],
                kv_name,
                e,
            )


def _reassociate_eips_on_start(s, project_id, topology, host):
    """Re-associate EIPs and sync security group rules on project start.

    Returns the (possibly updated) topology dict.
    """
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

    if not project_eips:
        return topology

    import json

    from sqlalchemy import text

    from app.models.project import Project

    s.execute(
        text("UPDATE projects SET topology = :topo WHERE id = :pid"),
        {"topo": json.dumps(topology), "pid": project_id},
    )
    s.commit()
    project = s.query(Project).filter_by(id=project_id).first()
    s.refresh(project)
    topology = project.topology or {}

    _sync_sg_rules_for_start(s, project, host, topology, project_id)
    return topology


def _sync_sg_rules_for_start(s, project, host, topology, project_id):
    """Sync security group rules for port forwards after EIP re-association."""
    from app.models.provider import Provider
    from app.services.eip_service import sync_security_group_rules

    provider = (
        s.query(Provider).filter_by(id=project.provider_id).first()
        if project.provider_id
        else None
    )
    if not provider and host.provider_id:
        provider = s.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return
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
    if not gw_node:
        return
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


def _finalize_project_active(s, project, project_id, topology):
    """Set project to active state, reset timers, and notify."""
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


def _start_troshkad_host_project(s, project, host, project_id):
    """Restart a stopped project on a troshkad-managed host.

    Returns True on success, False on error (project state already set).
    """
    topology = project.topology or {}
    vni_map = project.vni_map or {}

    topology = _reassociate_eips_on_start(s, project_id, topology, host)

    if vni_map:
        with _get_network_lock(host.id):
            net_result = _setup_networks_via_troshkad(
                host, topology, vni_map, s, project_id
            )
        if net_result is not True:
            project.state = "error"
            project.deploy_error = f"Network setup failed on restart: {net_result}"
            s.commit()
            return False

    _prepare_topology_library_refs(topology, s, project)
    cache_library_images(topology, host, s)
    _setup_pxe_via_troshkad(host, topology, vni_map, project_id)

    start_failures = _start_vms_via_troshkad(host, project_id, topology)
    if start_failures:
        failed_names = ", ".join(name for name, _ in start_failures)
        error_msg = f"Failed to start VMs: {failed_names}"
        logger.error("Start %s: %s", project_id[:8], error_msg)
        _set_project_error(s, project_id, error_msg, project=project)
        return False

    bmc_config = _extract_bmc_config(topology, project_id)
    if bmc_config:
        logger.info("Start %s: re-starting BMC endpoints", project_id[:8])
        try:
            _setup_bmc_via_troshkad(host, project_id, bmc_config)
        except Exception:
            logger.warning("Start %s: BMC setup failed (non-fatal)", project_id[:8])

    _finalize_project_active(s, project, project_id, topology)
    logger.info("Start %s: complete", project_id[:8])
    return True


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
            _set_project_error(
                s,
                project_id,
                "Host is disconnected or unavailable — cannot start VMs",
                project=project,
            )
            return

        if host.host_type == "kubevirt-cluster":
            topology = project.topology or {}
            vms = _extract_vms(topology)
            _start_kubevirt_vms(s, host, project_id, vms)
            _finalize_project_active(s, project, project_id, topology)
            logger.info("Start %s: kubevirt VMs started", project_id[:8])
            return

        if not host.ip_address:
            _set_project_error(
                s,
                project_id,
                "Host is disconnected or unavailable — cannot start VMs",
                project=project,
            )
            return

        _start_troshkad_host_project(s, project, host, project_id)

    except Exception:
        logger.exception("Start %s failed", project_id[:8])
        try:
            _set_project_error(
                s,
                project_id,
                "Start failed unexpectedly. Check server logs.",
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


def _wait_for_namespace_deletion(provider, project_id):
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

    _wait_for_namespace_deletion(provider, project_id)
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
            job_id = start_job(host, _VMS_DESTROY_PATH, {"domain_name": vm_name})
            wait_for_job(host, job_id, timeout=60)
        except TroshkadError as e:
            logger.warning(
                "Destroy %s: failed to destroy %s: %s", project_id[:8], vm_name, e
            )

    # Remove project VM directory
    pool = _get_host_pool(host, session)
    vm_dir = _vm_dir(project_id, pool)

    # Undefine the per-project storage pool libvirt/virt-install auto-created for
    # the VM disk directory. Do this before deleting the dir so it deactivates
    # cleanly; leaked pools accumulate and wedge virt-install on new deploys.
    try:
        job_id = start_job(host, "/pools/cleanup", {"target_dir": vm_dir})
        wait_for_job(host, job_id, timeout=20)
    except TroshkadError as e:
        logger.warning("Destroy %s: pool cleanup failed: %s", project_id[:8], e)

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


def _get_connected_host(session, host_id):
    from app.models.host import Host

    host = session.query(Host).filter_by(id=host_id).first()
    if host and host.agent_status == "connected":
        return host
    return None


def _destroy_stop_all_vms(session, project_id, unique_host_ids):
    logger.info("Destroy %s: stopping VMs on all hosts", project_id[:8])
    for host_id in unique_host_ids:
        host = _get_connected_host(session, host_id)
        if not host:
            logger.warning(
                "Destroy %s: host %s not connected", project_id[:8], host_id[:8]
            )
            continue
        try:
            job_id = start_job(host, "/vms/stop-all", {"project_id": project_id})
            wait_for_job(host, job_id, timeout=120)
        except Exception as e:
            logger.warning("Failed to stop VMs on host %s: %s", host_id[:8], e)


def _destroy_remote_networks(
    session, project_id, unique_host_ids, network_host_id, vni_map
):
    logger.info("Destroy %s: tearing down remote networks", project_id[:8])
    vni_list = list(vni_map.values())
    for host_id in unique_host_ids:
        if host_id == network_host_id:
            continue
        host = _get_connected_host(session, host_id)
        if not host:
            continue
        try:
            job_id = start_job(
                host,
                "/networks/full-teardown",
                {"project_id": project_id, "vni_list": vni_list},
            )
            wait_for_job(host, job_id, timeout=120)
        except Exception as e:
            logger.warning(
                "Failed to tear down remote network on %s: %s", host_id[:8], e
            )


def _destroy_mesh_on_all_hosts(session, project_id, unique_host_ids):
    logger.info("Destroy %s: tearing down mesh on all hosts", project_id[:8])
    for host_id in unique_host_ids:
        host = _get_connected_host(session, host_id)
        if not host:
            continue
        try:
            troshkad_request(host, "DELETE", f"/mesh/teardown?project_id={project_id}")
        except Exception as e:
            logger.warning("Failed to teardown mesh on %s: %s", host_id[:8], e)


def _destroy_multihost(session, project):
    """Destroy a multi-host project: VMs, networks, mesh."""
    project_id = project.id
    host_assignments = project.host_assignments or {}
    network_host_id = project.mesh_network_host_id
    vni_map = project.vni_map or {}

    unique_host_ids = set(host_assignments.values()) if host_assignments else set()
    if project.host_id:
        unique_host_ids.add(project.host_id)

    _destroy_stop_all_vms(session, project_id, unique_host_ids)
    _destroy_remote_networks(
        session, project_id, unique_host_ids, network_host_id, vni_map
    )

    logger.info("Destroy %s: tearing down network host", project_id[:8])
    if network_host_id:
        network_host = _get_connected_host(session, network_host_id)
        if network_host:
            with _get_network_lock(network_host.id):
                _teardown_networks_via_troshkad(network_host, project_id, vni_map)

    _destroy_mesh_on_all_hosts(session, project_id, unique_host_ids)

    logger.info("Destroy %s: cleaning up mesh DB entries", project_id[:8])
    delete_mesh_peers(session, project_id)


def _destroy_cleanup_dns(s, ctx, project_id):
    """Delete DNS records if the project has a DNS provider configured."""
    if not ctx.get("dns_provider_id"):
        return
    from app.models.dns_provider import DnsProvider
    from app.services.dns_service import delete_dns_records

    topo = ctx.get("topology", {})
    dns_provider = s.query(DnsProvider).filter_by(id=ctx["dns_provider_id"]).first()
    dns_records = topo.get("_dns_records", [])
    if dns_provider and dns_records:
        logger.info("Teardown %s: deleting DNS records", project_id[:8])
        delete_dns_records(dns_provider.type, dns_provider.config, dns_records)


def _destroy_cleanup_eips(s, project_id):
    """Release all EIPs for this project."""
    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import release_eip

    project_eips = s.query(ElasticIp).filter_by(project_id=project_id).all()
    for eip in project_eips:
        try:
            release_eip(s, eip)
        except Exception:
            logger.warning("Failed to release EIP %s on destroy", eip.public_ip)


def _destroy_revoke_ops_pod_key(s, project_id: str) -> None:
    """Deactivate the project-scoped ops-pod API key on teardown (best-effort).

    Mirrors :func:`_deploy_ops_pod`'s ``mint_ops_pod_key``: once the ops pod is
    gone its ``trk_`` credential must not outlive it. Idempotent — revoke returns
    0 when there is no ops-pod key (non-OCP or bastion projects) — and wrapped so
    a revoke failure never breaks teardown.
    """
    from app.services.ocp.ops_pod_auth import revoke_ops_pod_key

    try:
        revoked = revoke_ops_pod_key(s, project_id)
        if revoked:
            logger.debug(
                "Destroy %s: revoked %d ops-pod key(s)", project_id[:8], revoked
            )
    except Exception:
        logger.debug(
            "Destroy %s: ops-pod key revoke failed (non-fatal)",
            project_id[:8],
            exc_info=True,
        )


def _destroy_project_inner(ctx: dict, *, delete_record: bool = True):
    """Orchestrate project destruction by delegating to focused helper functions."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project

    project_id = ctx["project_id"]
    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project:
            if delete_record:
                _delete_project_record(project_id)
            return

        # Revoke the scoped ops-pod key before any teardown branch (covers the
        # KubeVirt-native early return below); no-op for non-ops-pod projects.
        _destroy_revoke_ops_pod_key(s, project_id)

        # Multi-host project: delegate to multi-host destroy path
        if project.mesh_subnet_id:
            logger.info("Destroy %s: multi-host project detected", project_id[:8])
            _destroy_multihost(s, project)
            # Continue with common cleanup (DNS, EIPs, etc.)
            # Fall through to common cleanup section below
        else:
            # Single-host project: existing path
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

            # Clean up security group rules for this project
            _destroy_cleanup_sg_rules(host, project_id, s)

            # Clean up Route-based external access (OCP Virt only)
            _destroy_cleanup_route_access(host, project_id, s)

        # Common cleanup for both single-host and multi-host projects
        _destroy_cleanup_dns(s, ctx, project_id)
        _destroy_cleanup_eips(s, project_id)

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
