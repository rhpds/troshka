"""
Pattern service — captures VM disk snapshots to S3 for pattern storage.
"""

import logging
import os

from app.core.database import SessionLocal
from app.core.redis import enqueue_job
from app.models.pattern import Pattern, PatternDisk
from app.services.pattern_sync import sync_pattern_to_central

log = logging.getLogger(__name__)


def _set_capture_progress(pattern_id: str, data: dict):
    from app.core.redis import set_progress

    set_progress(f"pattern-capture:{pattern_id}", data)


def _clear_capture_progress(pattern_id: str):
    from app.core.redis import delete_progress

    delete_progress(f"pattern-capture:{pattern_id}")


def _run_recert_force_expire(host, pattern_id, topology, disks, db):
    """Run recert --force-expire on the RHCOS boot disk to expire all certs.

    Called after pattern capture when pattern.recert is True. Operates on the
    flattened pattern disk in the local cache. Non-fatal — pattern is still
    marked available if this fails, but recert flag is cleared.
    """
    from app.services.troshkad_client import start_job, wait_for_job

    vms = [n for n in topology.get("nodes", []) if n.get("type") == "vmNode"]
    rhcos_vms = [v for v in vms if v.get("data", {}).get("os") == "rhcos"]
    if len(rhcos_vms) != 1:
        log.warning(
            "Pattern %s: recert requested but %d RHCOS VMs (need exactly 1), skipping",
            pattern_id[:8],
            len(rhcos_vms),
        )
        return

    rhcos_vm = rhcos_vms[0]
    rhcos_disk = None
    for d in disks:
        if d.source_vm_id == rhcos_vm["id"] and d.format == "qcow2":
            rhcos_disk = d
            break
    if not rhcos_disk:
        log.warning("Pattern %s: no RHCOS qcow2 disk found for recert", pattern_id[:8])
        return

    cache_path = f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{rhcos_disk.id}.{rhcos_disk.format}"

    log.info(
        "Pattern %s: running recert --force-expire on %s", pattern_id[:8], cache_path
    )
    try:
        job_id = start_job(
            host,
            "/vms/recert",
            {"disk": cache_path, "force_expire": True, "extend_expiration": False},
        )
        job = wait_for_job(host, job_id, timeout=300)
        if job.get("status") == "completed":
            log.info("Pattern %s: recert force-expire completed", pattern_id[:8])
        else:
            log.warning(
                "Pattern %s: recert force-expire failed: %s — clearing recert flag",
                pattern_id[:8],
                job.get("result", {}).get("error", "unknown"),
            )
            pattern = db.query(Pattern).filter_by(id=pattern_id).first()
            if pattern:
                pattern.recert = False
    except Exception:
        log.warning(
            "Pattern %s: recert force-expire error — clearing recert flag",
            pattern_id[:8],
            exc_info=True,
        )
        pattern = db.query(Pattern).filter_by(id=pattern_id).first()
        if pattern:
            pattern.recert = False


def _quiesce_ocp_cluster(host, project_id, topology, pattern_id):
    """Wait for OCP cluster to be in a clean state before pattern capture.

    Approves pending CSRs, forces kube-apiserver rollout, waits for all
    cluster operators to be Available and not Progressing.
    """
    from app.services.deploy_service import (
        _approve_pending_csrs,
        _exec_on_bastion,
        _is_ocp_topology,
    )

    if not _is_ocp_topology(topology):
        return

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

    log.info("Pattern %s: quiescing OCP cluster before capture", pattern_id[:8])
    _set_capture_progress(
        pattern_id,
        {
            "step": "quiescing",
            "detail": "Checking cluster health",
        },
    )
    from app.services.ws_pubsub import notify_pattern

    notify_pattern(
        pattern_id,
        {
            "type": "capture-progress",
            "step": "quiescing",
            "detail": "Checking cluster health",
        },
    )

    # Approve any pending CSRs
    approved = _approve_pending_csrs(host, project_id, bastion_ip, password)
    if approved:
        log.info("Pattern %s: approved %d CSR(s)", pattern_id[:8], approved)
        # Force kube-apiserver rollout only if certs rotated — the rollout
        # itself causes temporary auth disruption, so skip it if unnecessary
        _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            'oc patch kubeapiserver cluster --type=merge -p \'{"spec":{"forceRedeploymentReason":"pattern-capture-\'$(date +%s)\'"}}\' 2>/dev/null',
            timeout=10,
        )
        log.info(
            "Pattern %s: triggered kube-apiserver rollout after CSR approval",
            pattern_id[:8],
        )

    # Wait for all cluster operators to be Available and not Progressing
    import time

    deadline = time.time() + 300
    while time.time() < deadline:
        result = _exec_on_bastion(
            host,
            project_id,
            bastion_ip,
            password,
            "oc get co --no-headers 2>/dev/null | awk '{print $3,$4}' | sort | uniq -c",
            timeout=15,
        )
        if result:
            lines = result.strip().split("\n")
            all_good = all("True False" in line for line in lines if line.strip())
            if all_good:
                log.info(
                    "Pattern %s: cluster quiesced — all operators available",
                    pattern_id[:8],
                )
                _set_capture_progress(
                    pattern_id,
                    {
                        "step": "quiescing",
                        "detail": "Cluster stable — all operators available",
                    },
                )
                notify_pattern(
                    pattern_id,
                    {
                        "type": "capture-progress",
                        "step": "quiescing",
                        "detail": "Cluster stable — all operators available",
                    },
                )
                break
        _set_capture_progress(
            pattern_id,
            {
                "step": "quiescing",
                "detail": "Waiting for cluster operators to stabilize",
            },
        )
        notify_pattern(
            pattern_id,
            {
                "type": "capture-progress",
                "step": "quiescing",
                "detail": "Waiting for cluster operators to stabilize",
            },
        )
        time.sleep(10)
    else:
        log.warning(
            "Pattern %s: quiesce timed out after 5m, proceeding with capture",
            pattern_id[:8],
        )

    # Final CSR sweep
    _approve_pending_csrs(host, project_id, bastion_ip, password)


def _get_pattern_buffer(db, host):
    """Get the pattern buffer host for the pool this host belongs to, if any."""
    if not host.storage_pool_id:
        return None
    from app.services.pattern_buffer_service import get_pattern_buffer_host

    return get_pattern_buffer_host(db, host.storage_pool_id)


def _poll_job_with_progress(host, job_id, log_fn, timeout=3600, poll_interval=5):
    """Poll a troshkad job and forward the latest output line to log_fn."""
    import time

    from app.services.troshkad_client import TroshkadError, poll_job

    deadline = time.time() + timeout
    last_output_len = 0
    while time.time() < deadline:
        try:
            job = poll_job(host, job_id)
        except TroshkadError:
            time.sleep(poll_interval)
            continue
        output = job.get("output", [])
        if len(output) > last_output_len:
            latest = output[-1]
            if "Flatten" in latest or "Upload" in latest or "Cach" in latest:
                log_fn(latest)
            last_output_len = len(output)
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(poll_interval)
    raise TroshkadError(f"Job {job_id} timed out after {timeout}s")


def _capture_vm_via_nbd(
    host, worker_host, vm_id, domain_name, disks_params, creds, pattern_id, job_log_fn
):
    """Capture a VM's disks via NBD export (VM host) + pull-flatten (pattern buffer)."""
    from app.services.troshkad_client import start_job, wait_for_job

    results = []
    for i, disk_info in enumerate(disks_params):
        disk_path = disk_info["disk_path"]
        s3_url = disk_info["s3_url"]
        cache_path = disk_info["cache_path"]

        job_log_fn(f"Exporting {os.path.basename(disk_path)} via NBD...")
        export_job_id = start_job(
            host,
            "/nbd/export",
            {
                "domain_name": domain_name,
                "disk_path": disk_path,
            },
        )
        export_job = wait_for_job(host, export_job_id, timeout=120)
        if export_job["status"] != "completed":
            raise RuntimeError(
                f"NBD export failed: {export_job.get('result', {}).get('error')}"
            )
        nbd_port = export_job["result"]["port"]

        try:
            output_filename = f"{pattern_id[:8]}-{vm_id[:8]}-{i}.qcow2"
            output_path = f"/var/lib/troshka/local/tmp/{output_filename}"

            job_log_fn("Flattening...")
            flatten_job_id = start_job(
                worker_host,
                "/nbd/pull-flatten",
                {
                    "nbd_host": host.private_ip,
                    "nbd_port": nbd_port,
                    "export_name": "disk",
                    "output_path": output_path,
                    "total_bytes": 0,
                },
            )
            flatten_job = _poll_job_with_progress(
                worker_host, flatten_job_id, job_log_fn, timeout=3600
            )
            if flatten_job["status"] != "completed":
                raise RuntimeError(
                    f"Pull-flatten failed: {flatten_job.get('result', {}).get('error')}"
                )
            flat_size = flatten_job["result"].get("size_bytes", 0)

            job_log_fn(f"Uploading {round(flat_size / (1024**3), 1)} GB to S3...")
            upload_job_id = start_job(
                worker_host,
                "/patterns/upload-and-cache",
                {
                    "local_path": output_path,
                    "s3_url": s3_url,
                    "cache_path": cache_path,
                    "aws_access_key_id": creds.get("access_key_id", ""),
                    "aws_secret_access_key": creds.get("secret_access_key", ""),
                    "aws_region": creds.get("region", "us-east-1"),
                    "aws_endpoint_url": creds.get("endpoint_url", ""),
                },
            )
            upload_job = _poll_job_with_progress(
                worker_host, upload_job_id, job_log_fn, timeout=3600
            )
            if upload_job["status"] != "completed":
                raise RuntimeError(
                    f"Upload failed: {upload_job.get('result', {}).get('error')}"
                )

            results.append({"size_bytes": flat_size})
        finally:
            try:
                stop_job_id = start_job(
                    host,
                    "/nbd/stop",
                    {
                        "domain_name": domain_name,
                        "port": nbd_port,
                    },
                )
                wait_for_job(host, stop_job_id, timeout=600)
            except Exception as e:
                log.warning(
                    "NBD stop failed for %s port %d: %s", domain_name, nbd_port, e
                )

    return results


def get_capture_progress(pattern_id: str) -> dict | None:
    """Return capture progress for a pattern, or None if not tracking."""
    from app.core.redis import get_progress

    return get_progress(f"pattern-capture:{pattern_id}")


def cancel_capture(pattern_id: str, db) -> None:
    """Cancel in-flight capture jobs on the host or KubeVirt cluster."""
    from app.models.host import Host
    from app.models.pattern import Pattern
    from app.models.project import Project

    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern or not pattern.source_project_id:
        _clear_capture_progress(pattern_id)
        return

    project = db.query(Project).filter_by(id=pattern.source_project_id).first()
    host = db.query(Host).filter_by(id=project.host_id).first() if project else None

    if host and host.host_type == "kubevirt-cluster":
        _cancel_kubevirt_capture(pattern_id, host, project, db)
    else:
        _cancel_troshkad_capture(pattern_id, host, db)

    _clear_capture_progress(pattern_id)


def _cancel_troshkad_capture(pattern_id, host, db):
    """Cancel troshkad-based capture jobs."""
    from app.services.troshkad_client import TroshkadError, cancel_job

    progress = get_capture_progress(pattern_id)
    if not progress or not host:
        return
    for job_id in progress.get("_job_ids", []):
        try:
            cancel_job(host, job_id)
            log.info(
                "Cancelled capture job %s on host %s for pattern %s",
                job_id[:8],
                host.id[:8],
                pattern_id[:8],
            )
        except TroshkadError:
            pass


def _cancel_kubevirt_capture(pattern_id, host, project, db):
    """Cancel KubeVirt capture: clear annotation, delete export jobs."""
    from app.models.provider import Provider
    from app.services.providers.kubevirt import (
        CRD_GROUP,
        CRD_VERSION,
        _get_k8s_clients,
        _project_ns,
    )

    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return

    custom_api, _, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project.id)
    cr_name = f"project-{project.id[:8]}"

    try:
        custom_api.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkaprojects",
            name=cr_name,
            body={
                "metadata": {
                    "annotations": {"troshka.redhat.com/capture-request": None}
                }
            },
        )
    except Exception:
        log.debug("Failed to clear capture annotation for %s", pattern_id[:8])

    from kubernetes import client as k8s_client

    batch_api = k8s_client.BatchV1Api(custom_api.api_client)
    try:
        jobs = batch_api.list_namespaced_job(
            namespace=namespace, label_selector="troshka-role=pattern-export"
        )
        for j in getattr(jobs, "items", []):
            try:
                batch_api.delete_namespaced_job(
                    name=j.metadata.name,
                    namespace=namespace,
                    propagation_policy="Background",
                )
            except Exception:
                pass
    except Exception:
        log.debug("Failed to delete export jobs for %s", pattern_id[:8])

    log.info("Cancelled KubeVirt capture for pattern %s", pattern_id[:8])


def _build_capture_disk_manifest(disk_nodes, disk_to_vm, pattern_id, vm_nodes=None):
    """Build the disk manifest list for a KubeVirt capture request."""
    manifest = []
    for disk_node in disk_nodes:
        fmt = disk_node.get("data", {}).get("format", "qcow2")
        if fmt == "iso":
            continue
        vm_id = disk_to_vm.get(disk_node["id"], "")
        if not vm_id:
            continue
        vm_name = f"vm-{vm_id[:8]}"
        disk_id = disk_node["id"]
        disk_label = disk_node.get("data", {}).get(
            "label", disk_node.get("data", {}).get("name", disk_id[:8])
        )
        vm_label = ""
        if vm_nodes and vm_id in vm_nodes:
            vm_label = (
                vm_nodes[vm_id]
                .get("data", {})
                .get("label", vm_nodes[vm_id].get("data", {}).get("name", ""))
            )
        manifest.append(
            {
                "vmName": vm_name,
                "vmId": vm_id,
                "diskId": disk_id,
                "pvcName": f"{vm_name}-disk-{disk_id[:8]}",
                "s3Key": f"patterns/{pattern_id}/{disk_id}.{fmt}",
                "sizeGb": int(disk_node.get("data", {}).get("size", 50)),
                "format": fmt,
                "diskLabel": disk_label,
                "vmLabel": vm_label,
            }
        )
    return manifest


def _poll_capture_completion(
    custom_api,
    namespace,
    cr_name,
    pattern_id,
    crd_group,
    crd_version,
    max_wait_seconds=2700,
):
    """Poll CR status for capture completion.

    max_wait_seconds defaults to 45 min but callers should scale it
    based on total disk size for large captures.
    Returns the captured disks list on success, or None on error/timeout.
    """
    import time as _time

    from app.services.ws_pubsub import notify_pattern

    iterations = max_wait_seconds // 10
    for _attempt in range(iterations):
        _time.sleep(10)
        from app.core.database import SessionLocal as _SL

        _check_db = _SL()
        _exists = _check_db.query(Pattern).filter_by(id=pattern_id).first() is not None
        _check_db.close()
        if not _exists:
            log.info("Pattern %s: deleted during capture, exiting poll", pattern_id[:8])
            return None
        try:
            cr_obj = custom_api.get_namespaced_custom_object(
                group=crd_group,
                version=crd_version,
                namespace=namespace,
                plural="troshkaprojects",
                name=cr_name,
            )
            cr: dict = cr_obj if isinstance(cr_obj, dict) else {}
            cr_status: dict = cr.get("status") or {}
            phase = cr_status.get("phase", "")
            progress = cr_status.get("captureProgress", "")

            disks = cr_status.get("captureDisks") or []
            progress_data: dict = {
                "step": "capturing",
                "detail": progress,
            }
            if disks:
                progress_data["disks"] = disks
            _set_capture_progress(pattern_id, progress_data)
            notify_pattern(
                pattern_id,
                {
                    "type": "capture-progress",
                    "step": "capturing",
                    "detail": progress,
                    "disks": disks,
                },
            )

            if phase == "CaptureComplete":
                captured_disks = cr_status.get("capturedDisks", [])
                log.info(
                    "Capture complete for %s: %d disks",
                    pattern_id[:8],
                    len(captured_disks),
                )
                return captured_disks

            if phase == "CaptureError":
                err = cr_status.get("captureError", "Unknown error")
                log.error("Capture failed for %s: %s", pattern_id[:8], err)
                return None

        except Exception as e:
            log.warning("Error polling capture status for %s: %s", pattern_id[:8], e)

    log.error("Capture timed out for %s after 45 minutes", pattern_id[:8])
    return None


def _save_pattern_metadata_to_s3(pattern, pattern_id):
    """Write the canonical pattern metadata.json to central S4.

    Used by BOTH capture paths so the gold copy always carries the topology and
    each disk's full identity (``id`` + ``source_disk_id``). Cross-cluster
    import (``central_library``) and DR recovery rebuild the pattern from this
    object, so a thin copy (no topology / no ids) yields an unusable import.
    """
    import json as _json

    from app.services import s3_storage

    metadata = {
        "type": "pattern",
        "pattern_id": pattern_id,
        "name": pattern.name,
        "description": pattern.description,
        "visibility": pattern.visibility,
        "topology": pattern.topology,
        "total_size_bytes": pattern.total_size_bytes,
        "tags": pattern.tags,
        "disks": [
            {
                "id": d.id,
                "source_disk_id": d.source_disk_id,
                "source_vm_id": d.source_vm_id,
                "s3_key": d.s3_key,
                "format": d.format,
                "size_bytes": d.size_bytes,
                "virtual_size_bytes": d.virtual_size_bytes,
            }
            for d in pattern.disks
        ],
    }
    try:
        s3_storage._get_s3_client().put_object(
            Bucket=s3_storage._bucket(),
            Key=f"patterns/{pattern_id}/metadata.json",
            Body=_json.dumps(metadata).encode(),
            ContentType="application/json",
        )
    except Exception:
        log.warning("Failed to save pattern metadata.json for %s", pattern_id[:8])


def _update_topology_with_captures(topo, captured_disks, pattern_id, pd_id_by_disk_id):
    """Update topology storage nodes to reference captured pattern disks.

    ``pd_id_by_disk_id`` maps each captured disk's content id (``diskId``,
    equal to the storage node id and ``PatternDisk.source_disk_id``) to the
    ``PatternDisk.id``. ``patternDiskId`` MUST be the PatternDisk row id —
    that is what ``PatternLocation`` FKs to and what deploy placement uses to
    check disk availability. Writing the content id here silently breaks every
    pattern-derived deploy with a misleading 'not enough capacity' error.
    """
    for node in topo.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        if node.get("data", {}).get("format") == "iso":
            continue
        node_id = node["id"]
        matching_pd = next(
            (cd for cd in captured_disks if cd.get("diskId") == node_id), None
        )
        if matching_pd:
            node["data"]["source"] = "pattern"
            node["data"]["patternId"] = pattern_id
            node["data"]["patternDiskId"] = pd_id_by_disk_id.get(node_id, node_id)
            node["data"].pop("libraryItemId", None)
            node["data"].pop("libraryItemName", None)


def _restart_kubevirt_vms(custom_api, namespace):
    """Restart all KubeVirt VMs in the given namespace."""
    try:
        vms_obj = custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
        )
        vms_list: dict = vms_obj if isinstance(vms_obj, dict) else {}
        for vm in vms_list.get("items", []):
            custom_api.patch_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=vm["metadata"]["name"],
                body={"spec": {"running": True}},
            )
    except Exception as e:
        log.warning("Failed to restart VMs after capture: %s", e)


def _enqueue_pattern_sync(pattern_id: str, source_provider_id: str | None) -> None:
    """Kick off eager OBC->central S4 replication for a freshly captured pattern."""
    if not source_provider_id:
        return
    enqueue_job(sync_pattern_to_central, pattern_id, queue_name="default")


def _capture_kubevirt_native(db, pattern, project, host, restart_after):
    """Capture pattern disks via KubeVirt VolumeSnapshot + S3 export Jobs."""
    import json as _json

    from app.models.provider import Provider
    from app.services.providers.kubevirt import (
        CRD_GROUP,
        CRD_VERSION,
        _ensure_s3_secret,
        _get_k8s_clients,
        _project_ns,
    )
    from app.services.ws_pubsub import notify_pattern

    pattern_id = pattern.id
    project_id = project.id

    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        pattern.state = "error"
        db.commit()
        log.error("Pattern %s: provider not found", pattern_id[:8])
        return

    pattern.source_provider_id = provider.id

    custom_api, _core_api, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)

    from app.services.s3_storage import get_cluster_s3_config

    cluster_s3 = get_cluster_s3_config(db, provider.id)
    if not cluster_s3:
        pattern.state = "error"
        db.commit()
        log.error(
            "Pattern %s: no OBC credentials for provider %s",
            pattern_id[:8],
            provider.name,
        )
        return

    s3_config_for_secret = {
        "access_key_id": cluster_s3.get("access_key_id", ""),
        "secret_access_key": cluster_s3.get("secret_access_key", ""),
        "region": cluster_s3.get("region", "us-east-1"),
        "endpoint_url": cluster_s3.get("endpoint", ""),
    }
    capture_s3 = {
        "bucket": cluster_s3.get("bucket", ""),
        "endpoint": cluster_s3.get("endpoint", ""),
        "region": cluster_s3.get("region", "us-east-1"),
        "credentialsSecret": "s3-credentials",  # pragma: allowlist secret
    }

    _ensure_s3_secret(provider, namespace, s3_config_for_secret)

    topology = project.deployed_topology or project.topology or {}
    disk_nodes, vm_nodes, disk_to_vm, _ = _build_disk_to_vm_map(topology)

    disk_manifest = _build_capture_disk_manifest(
        disk_nodes, disk_to_vm, pattern_id, vm_nodes
    )
    if not disk_manifest:
        pattern.state = "error"
        db.commit()
        log.error("Pattern %s: no disks to capture", pattern_id[:8])
        return

    capture_config = {
        "patternId": pattern_id,
        "s3Config": capture_s3,
        "disks": disk_manifest,
        "restartAfter": restart_after,
    }

    _set_capture_progress(
        pattern_id,
        {
            "step": "capturing",
            "detail": "Triggering capture on cluster",
        },
    )
    notify_pattern(
        pattern_id,
        {
            "type": "capture-progress",
            "step": "capturing",
            "detail": "Triggering capture on cluster",
        },
    )

    cr_name = f"project-{project_id[:8]}"
    try:
        # Clear stale capture status before starting
        custom_api.patch_namespaced_custom_object_status(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkaprojects",
            name=cr_name,
            body={
                "status": {
                    "phase": "Active",
                    "captureProgress": None,
                    "captureError": None,
                    "captureDisks": None,
                    "capturedDisks": None,
                }
            },
        )
        custom_api.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkaprojects",
            name=cr_name,
            body={
                "metadata": {
                    "annotations": {
                        "troshka.redhat.com/capture-request": _json.dumps(
                            capture_config
                        )
                    }
                }
            },
        )
    except Exception as e:
        log.exception("Failed to trigger capture on %s: %s", cr_name, e)
        pattern.state = "error"
        db.commit()
        return

    total_disk_gb = sum(d.get("sizeGb", 50) for d in disk_manifest)
    poll_timeout = max(2700, total_disk_gb * 30)

    # Release the DB transaction before the long poll so cancel/delete
    # can modify the pattern row without blocking on our lock.
    db.commit()
    db.expire_all()

    captured_disks = _poll_capture_completion(
        custom_api,
        namespace,
        cr_name,
        pattern_id,
        CRD_GROUP,
        CRD_VERSION,
        max_wait_seconds=poll_timeout,
    )

    # Re-query pattern after poll — it may have been deleted by cancel
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        log.info("Pattern %s deleted during capture", pattern_id[:8])
        _clear_capture_progress(pattern_id)
        return

    if captured_disks is None:
        pattern.state = "error"
        db.commit()
        log.error("Pattern %s: capture failed or timed out", pattern_id[:8])
        _clear_capture_progress(pattern_id)
        return

    # Create PatternDisk records from captured disks
    from app.models.pattern_location import PatternLocation

    total_size = 0
    pd_id_by_disk_id: dict[str, str] = {}
    for cd in captured_disks:
        pd = PatternDisk(
            pattern_id=pattern_id,
            source_disk_id=cd.get("diskId", ""),
            source_vm_id=cd.get("vmId", ""),
            s3_key=cd.get("s3Key", ""),
            format=cd.get("format", "qcow2"),
            size_bytes=cd.get("sizeBytes", 0),
            virtual_size_bytes=cd.get("virtualSizeBytes", 0),
            state="available",
        )
        db.add(pd)
        db.flush()
        pd_id_by_disk_id[cd.get("diskId", "")] = pd.id
        total_size += cd.get("sizeBytes", 0)

        # Track that this disk exists on the source cluster's RGW
        if pattern.source_provider_id:
            loc = PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=pattern.source_provider_id,
                s3_key=cd.get("s3Key", ""),
                state="synced",
                size_bytes=cd.get("sizeBytes", 0),
            )
            db.add(loc)

    # Update topology nodes to reference pattern (by PatternDisk.id)
    topo = pattern.topology or {}
    _update_topology_with_captures(topo, captured_disks, pattern_id, pd_id_by_disk_id)

    from sqlalchemy import text

    db.execute(
        text("UPDATE patterns SET topology = :topo WHERE id = :pid"),
        {"topo": _json.dumps(topo), "pid": pattern_id},
    )

    pattern.state = "available"
    pattern.total_size_bytes = total_size
    db.commit()

    _clear_capture_progress(pattern_id)
    notify_pattern(pattern_id, {"type": "capture-complete"})
    _enqueue_pattern_sync(pattern_id, pattern.source_provider_id)

    # Save the canonical metadata.json to central S4 (topology + full disk
    # identity) so cross-cluster import / DR recovery can rebuild the pattern.
    _save_pattern_metadata_to_s3(pattern, pattern_id)

    if restart_after:
        _restart_kubevirt_vms(custom_api, namespace)

    # Clear the capture annotation
    try:
        custom_api.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkaprojects",
            name=cr_name,
            body={
                "metadata": {
                    "annotations": {"troshka.redhat.com/capture-request": None}
                }
            },
        )
    except Exception:
        pass

    log.info(
        "Pattern %s capture complete: %d disks, %d bytes",
        pattern_id[:8],
        len(captured_disks),
        total_size,
    )


def _build_disk_to_vm_map(topology):
    """Extract disk-to-VM mapping from topology.

    Returns (disk_nodes, vm_nodes_by_id, disk_to_vm, vm_to_disks).
    """
    disk_nodes = [
        n for n in topology.get("nodes", []) if n.get("type") == "storageNode"
    ]
    vm_nodes = {
        n["id"]: n for n in topology.get("nodes", []) if n.get("type") == "vmNode"
    }
    edges = topology.get("edges", [])
    disk_to_vm: dict[str, str] = {}
    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src in vm_nodes and tgt in [d["id"] for d in disk_nodes]:
            disk_to_vm[tgt] = src
        elif tgt in vm_nodes and src in [d["id"] for d in disk_nodes]:
            disk_to_vm[src] = tgt

    vm_to_disks: dict[str, list] = {}
    for disk_node in disk_nodes:
        vm_id = disk_to_vm.get(disk_node["id"])
        if not vm_id:
            continue
        if vm_id not in vm_to_disks:
            vm_to_disks[vm_id] = []
        vm_to_disks[vm_id].append(disk_node)

    return disk_nodes, vm_nodes, disk_to_vm, vm_to_disks


def _build_nbd_vm_tasks(vm_to_disks, vm_nodes, project_id, pattern_id, pool):
    """Build the per-VM task list for NBD capture.

    Returns a list of dicts, each with vm_id, vm_name, domain_name,
    disks_params, and disk_metadata.
    """
    from app.services import s3_storage
    from app.services.deploy_topology import _disk_path

    vm_tasks = []
    for vm_id, vm_disk_nodes in vm_to_disks.items():
        disks_params = []
        disk_metadata = []
        for disk_node in vm_disk_nodes:
            disk_id = disk_node["id"]
            fmt = disk_node.get("data", {}).get("format", "qcow2")
            if fmt == "iso":
                continue
            disk_path = _disk_path(project_id, vm_id, disk_id, fmt, pool=pool)
            s3_key = f"patterns/{pattern_id}/{disk_id}.{fmt}"
            bucket = s3_storage._bucket()
            s3_url = f"s3://{bucket}/{s3_key}"
            cache_path = (
                f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{disk_id}.{fmt}"
            )
            vsize = int(disk_node.get("data", {}).get("size", 0)) * 1073741824
            disks_params.append(
                {
                    "disk_path": disk_path,
                    "s3_url": s3_url,
                    "cache_path": cache_path,
                    "virtual_size_bytes": vsize,
                }
            )
            disk_metadata.append(
                {
                    "disk_id": disk_id,
                    "vm_id": vm_id,
                    "s3_key": s3_key,
                    "format": fmt,
                    "virtual_size_bytes": vsize,
                }
            )
        if not disks_params:
            continue
        vm_name = vm_nodes.get(vm_id, {}).get("data", {}).get("label", vm_id[:8])
        domain_name = f"troshka-{project_id[:8]}-{vm_id[:8]}"
        vm_tasks.append(
            {
                "vm_id": vm_id,
                "vm_name": vm_name,
                "domain_name": domain_name,
                "disks_params": disks_params,
                "disk_metadata": disk_metadata,
            }
        )
    return vm_tasks


def _capture_via_nbd(
    host,
    worker_host,
    vm_to_disks,
    vm_nodes,
    project_id,
    pattern_id,
    creds,
    pool,
    pattern,
    db,
):
    """Capture disks via NBD export (VM host) + pull-flatten (pattern buffer).

    Returns True on success, False on error (pattern.state set to 'error').
    """
    import threading as _threading

    from app.services.ws_pubsub import notify_pattern

    log.info(
        "Pattern %s: using pattern buffer %s for NBD capture",
        pattern_id[:8],
        worker_host.id[:8],
    )

    vm_tasks = _build_nbd_vm_tasks(vm_to_disks, vm_nodes, project_id, pattern_id, pool)

    vm_count = len(vm_tasks)
    vm_status = {t["vm_id"]: "waiting" for t in vm_tasks}
    _set_capture_progress(
        pattern_id,
        {
            "step": "capturing",
            "detail": f"0/{vm_count} VMs done (NBD)",
            "_host_id": host.id,
            "_job_ids": [],
        },
    )

    def _update_progress():
        done = sum(1 for s in vm_status.values() if s == "done")
        lines = []
        for t in vm_tasks:
            s = vm_status[t["vm_id"]]
            lines.append(f"{t['vm_name']}: {s}")
        progress = {
            "step": "capturing",
            "detail": f"{done}/{vm_count} VMs done (NBD)",
            "vms": lines,
        }
        _set_capture_progress(pattern_id, progress)
        notify_pattern(pattern_id, {"type": "capture-progress", **progress})

    errors = {}
    results_map = {}

    def _capture_one_vm(task):
        vid = task["vm_id"]
        try:

            def _log(msg, _vid=vid):
                vm_status[_vid] = msg
                _update_progress()

            r = _capture_vm_via_nbd(
                host,
                worker_host,
                vid,
                task["domain_name"],
                task["disks_params"],
                creds,
                pattern_id,
                _log,
            )
            results_map[vid] = r
            vm_status[vid] = "done"
            _update_progress()
        except Exception as e:
            errors[vid] = str(e)
            vm_status[vid] = f"error: {e}"
            _update_progress()

    threads = []
    for task in vm_tasks:
        t = _threading.Thread(target=_capture_one_vm, args=(task,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=3600)

    if errors:
        log.error("NBD capture errors for pattern %s: %s", pattern_id[:8], errors)
        pattern.state = "error"
        db.commit()
        return False

    for task in vm_tasks:
        vid = task["vm_id"]
        nbd_results = results_map.get(vid, [])
        for j, metadata in enumerate(task["disk_metadata"]):
            size_bytes = (
                nbd_results[j].get("size_bytes", 0) if j < len(nbd_results) else 0
            )
            pd = PatternDisk(
                pattern_id=pattern_id,
                source_disk_id=metadata["disk_id"],
                source_vm_id=metadata["vm_id"],
                s3_key=metadata["s3_key"],
                format=metadata["format"],
                size_bytes=size_bytes,
                virtual_size_bytes=metadata["virtual_size_bytes"],
                state="available",
            )
            db.add(pd)
        db.commit()
        log.info("Pattern %s: VM %s NBD capture done", pattern_id[:8], vid[:8])

    return True


_PROGRESS_KEYWORDS = ("Flatten", "Upload", "Commit", "Snapshot", "Trim", "Cach")


def _poll_one_capture_job(host, jinfo, completed_jobs, poll_job, troshkad_error):
    """Poll a single capture job and return a status line string.

    If the job is already completed, returns immediately. On poll error,
    returns a 'polling...' status. For running jobs, extracts the last
    progress keyword from output.
    """
    vm_name = jinfo["vm_name"]
    if jinfo["job_id"] in completed_jobs:
        return f"{vm_name}: done"
    try:
        job = poll_job(host, jinfo["job_id"])
    except troshkad_error:
        return f"{vm_name}: polling..."
    if job["status"] in ("completed", "failed", "cancelled"):
        completed_jobs.add(jinfo["job_id"])
        jinfo["_result"] = job
        if job["status"] in ("failed", "cancelled"):
            return f"{vm_name}: {job['status'].upper()}"
        return f"{vm_name}: done"
    # Still running — find last progress keyword in output
    last = ""
    for line in reversed(job.get("output", [])):
        if any(kw in line for kw in _PROGRESS_KEYWORDS):
            last = line
            break
    return f"{vm_name}: {last}" if last else f"{vm_name}: working..."


def _resolve_job_result(jinfo, host):
    """Resolve the final job result for a capture job, polling if needed."""
    from app.services.troshkad_client import TroshkadError, poll_job

    job = jinfo.get("_result")
    if not job:
        try:
            job = poll_job(host, jinfo["job_id"])
        except TroshkadError:
            job = {"status": "failed", "result": {"error": "Job lost"}}
    if not job:
        job = {
            "status": "failed",
            "result": {"error": "Job result missing"},
        }
    return job


def _save_vm_disks(job, jinfo, pattern_id, db):
    """Create PatternDisk records from a successful capture job result."""
    disk_results = (job or {}).get("result", {}).get("disks", [])
    for j, metadata in enumerate(jinfo["disk_metadata"]):
        size_bytes = (
            disk_results[j].get("size_bytes", 0) if j < len(disk_results) else 0
        )
        pd = PatternDisk(
            pattern_id=pattern_id,
            source_disk_id=metadata["disk_id"],
            source_vm_id=metadata["vm_id"],
            s3_key=metadata["s3_key"],
            format=metadata["format"],
            size_bytes=size_bytes,
            virtual_size_bytes=metadata["virtual_size_bytes"],
            state="available",
        )
        db.add(pd)
    db.commit()


def _process_direct_capture_results(all_jobs, host, pattern_id, pattern, db):
    """Process results from direct capture jobs, creating PatternDisk records.

    Returns True if all VMs succeeded, False if any failed (sets pattern error state).
    """
    from app.services.troshkad_client import TroshkadError

    direct_errors: list[str] = []
    for jinfo in all_jobs:
        job = _resolve_job_result(jinfo, host)
        try:
            if job["status"] == "failed":
                error_msg = job.get("result", {}).get("error", "Pattern capture failed")
                log.error(
                    "Failed to capture pattern %s VM %s: %s",
                    pattern_id[:8],
                    jinfo["vm_id"][:8],
                    error_msg,
                )
                direct_errors.append(jinfo["vm_name"])
                continue

            _save_vm_disks(job, jinfo, pattern_id, db)
            log.info(
                "Pattern %s: VM %s capture done",
                pattern_id[:8],
                jinfo["vm_id"][:8],
            )

        except TroshkadError as e:
            log.exception(
                "Troshkad error capturing pattern %s VM %s: %s",
                pattern_id[:8],
                jinfo["vm_id"][:8],
                str(e),
            )
            direct_errors.append(jinfo["vm_name"])

    if direct_errors:
        log.error(
            "Pattern %s: %d VM(s) failed: %s",
            pattern_id[:8],
            len(direct_errors),
            ", ".join(direct_errors),
        )
        pattern.state = "error"
        db.commit()
        return False

    return True


def _capture_direct(
    host,
    vm_to_disks,
    vm_nodes,
    project_id,
    pattern_id,
    creds,
    pool,
    pattern,
    db,
):
    """Capture disks directly on the VM host (original flow).

    Returns True on success, False on error (pattern.state set to 'error').
    """
    import time as _time

    from app.services import s3_storage
    from app.services.deploy_topology import _disk_path
    from app.services.troshkad_client import TroshkadError, poll_job, start_job
    from app.services.ws_pubsub import notify_pattern

    all_jobs = []
    all_metadata = []
    for vm_id, vm_disk_nodes in vm_to_disks.items():
        disks_params = []
        disk_metadata = []
        for disk_node in vm_disk_nodes:
            disk_id = disk_node["id"]
            fmt = disk_node.get("data", {}).get("format", "qcow2")

            if fmt == "iso":
                continue

            disk_path = _disk_path(project_id, vm_id, disk_id, fmt, pool=pool)

            s3_key = f"patterns/{pattern_id}/{disk_id}.{fmt}"
            bucket = s3_storage._bucket()
            s3_url = f"s3://{bucket}/{s3_key}"
            cache_path = (
                f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{disk_id}.{fmt}"
            )

            vsize = int(disk_node.get("data", {}).get("size", 0)) * 1073741824
            disks_params.append(
                {
                    "disk_path": disk_path,
                    "s3_url": s3_url,
                    "cache_path": cache_path,
                    "virtual_size_bytes": vsize,
                }
            )

            disk_metadata.append(
                {
                    "disk_id": disk_id,
                    "vm_id": vm_id,
                    "s3_key": s3_key,
                    "format": fmt,
                    "virtual_size_bytes": int(disk_node.get("data", {}).get("size", 0))
                    * 1073741824,
                }
            )

        if not disks_params:
            continue

        try:
            domain_name = f"troshka-{project_id[:8]}-{vm_id[:8]}"
            job_id = start_job(
                host,
                "/patterns/capture-direct",
                {
                    "disks": disks_params,
                    "domain_name": domain_name,
                    "aws_access_key_id": creds.get("access_key_id", ""),
                    "aws_secret_access_key": creds.get("secret_access_key", ""),
                    "aws_region": creds.get("region", "us-east-1"),
                    "aws_endpoint_url": creds.get("endpoint_url", ""),
                },
            )
            vm_name = vm_nodes.get(vm_id, {}).get("data", {}).get("label", vm_id[:8])
            all_jobs.append(
                {
                    "job_id": job_id,
                    "vm_id": vm_id,
                    "vm_name": vm_name,
                    "disks_params": disks_params,
                    "disk_metadata": disk_metadata,
                }
            )
            all_metadata.extend(disk_metadata)
            log.info(
                "Pattern %s: started capture job for VM %s (%d disks)",
                pattern_id[:8],
                vm_id[:8],
                len(disks_params),
            )
        except TroshkadError as e:
            log.exception(
                "Failed to start capture for pattern %s VM %s: %s",
                pattern_id[:8],
                vm_id[:8],
                e,
            )
            pattern.state = "error"
            db.commit()
            return False

    # Poll all jobs concurrently, update progress with per-VM status
    completed_jobs: set[str] = set()
    deadline = _time.time() + 3600
    _set_capture_progress(
        pattern_id,
        {
            "step": "capturing",
            "detail": f"0/{len(all_jobs)} VMs done",
            "_host_id": host.id,
            "_job_ids": [j["job_id"] for j in all_jobs],
        },
    )

    while len(completed_jobs) < len(all_jobs) and _time.time() < deadline:
        if not get_capture_progress(pattern_id):
            log.info(
                "Pattern %s: capture cancelled, exiting poll loop",
                pattern_id[:8],
            )
            return False
        lines = [
            _poll_one_capture_job(host, jinfo, completed_jobs, poll_job, TroshkadError)
            for jinfo in all_jobs
        ]
        progress = {
            "step": "capturing",
            "detail": f"{len(completed_jobs)}/{len(all_jobs)} VMs done",
            "vms": lines,
        }
        _set_capture_progress(pattern_id, progress)
        notify_pattern(pattern_id, {"type": "capture-progress", **progress})
        _time.sleep(5)

    # Process results — save successful VMs, skip failed ones
    return _process_direct_capture_results(all_jobs, host, pattern_id, pattern, db)


def _capture_container_images(host, topology, pattern_id, creds, pattern, db):
    """Capture container images to S3.

    Returns True on success, False on error (pattern.state set to 'error').
    """
    from app.services import s3_storage
    from app.services.deploy_topology import _extract_containers
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    containers = _extract_containers(topology)
    for ctr in containers:
        if not ctr["image"]:
            log.info(
                "Pattern %s: skipping container %s (no image)",
                pattern_id[:8],
                ctr["node_id"][:8],
            )
            continue

        ctr_id = ctr["node_id"]
        tar_filename = f"container-{ctr_id[:8]}-image.tar.gz"
        save_path = f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{tar_filename}"
        s3_key = f"patterns/{pattern_id}/{tar_filename}"

        log.info(
            "Pattern %s: saving container image %s...",
            pattern_id[:8],
            ctr["image"],
        )
        try:
            job_id = start_job(
                host,
                "/containers/save-image",
                {
                    "image": ctr["image"],
                    "output_path": save_path,
                },
            )
            wait_for_job(host, job_id, timeout=600)

            job_id = start_job(
                host,
                "/patterns/upload-and-cache",
                {
                    "local_path": save_path,
                    "s3_bucket": s3_storage._bucket(),
                    "s3_key": s3_key,
                    "cache_path": save_path,
                    "aws_access_key_id": creds.get("access_key_id", ""),
                    "aws_secret_access_key": creds.get("secret_access_key", ""),
                    "aws_region": creds.get("region", "us-east-1"),
                    "aws_endpoint_url": creds.get("endpoint_url", ""),
                },
            )
            wait_for_job(host, job_id, timeout=1200)
            log.info(
                "Pattern %s: container image %s saved to S3",
                pattern_id[:8],
                ctr["image"],
            )
        except TroshkadError as e:
            log.exception(
                "Failed to capture container image %s for pattern %s: %s",
                ctr["image"],
                pattern_id[:8],
                str(e),
            )
            pattern.state = "error"
            db.commit()
            return False

    return True


def _finalize_pattern_capture(pattern, pattern_id, worker_host, host, db):
    """Update topology, run recert, save metadata, and send completion notification."""
    import copy
    import json

    from sqlalchemy import text

    from app.services.ws_pubsub import notify_pattern

    # Update pattern topology: point storage nodes to captured pattern disks
    topo = pattern.topology or {}
    disk_map = {d.source_disk_id: d for d in pattern.disks}
    for node in topo.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        if node.get("data", {}).get("format") == "iso":
            continue
        pd = disk_map.get(node["id"])  # type: ignore[assignment]
        if pd:
            node["data"]["source"] = "pattern"
            node["data"]["patternId"] = pattern_id
            node["data"]["patternDiskId"] = pd.id
            node["data"].pop("libraryItemId", None)

    db.execute(
        text("UPDATE patterns SET topology = :topo WHERE id = :pid"),
        {"topo": json.dumps(copy.deepcopy(topo)), "pid": pattern_id},
    )

    if pattern.recert:
        _run_recert_force_expire(
            worker_host or host, pattern_id, topo, pattern.disks, db
        )

    pattern.state = "available"
    pattern.total_size_bytes = sum(d.size_bytes for d in pattern.disks)
    db.commit()

    # Save the canonical metadata.json to central S4 for recovery after DB loss
    _save_pattern_metadata_to_s3(pattern, pattern_id)

    log.info("Pattern %s capture complete", pattern_id)

    # Touch pattern buffer activity
    if worker_host and worker_host.storage_pool_id:
        try:
            from app.services.pattern_buffer_service import touch_activity

            touch_activity(db, worker_host.storage_pool_id)
        except Exception:
            log.debug("Failed to touch PB activity", exc_info=True)

    _set_capture_progress(
        pattern_id,
        {
            "step": "complete",
            "detail": "Capture complete",
            "vms": [],
        },
    )
    notify_pattern(pattern_id, {"type": "capture-complete", "state": "available"})


def _run_capture_pipeline(
    db, pattern, host, worker_host, project, project_id, pattern_id, quiesce_cluster
):
    """Run the full disk + container capture pipeline.

    Returns True on success, False on failure (pattern state set to 'error'
    by the individual capture functions).
    """
    from app.services.s3_storage import _get_s3_config

    topology = (
        project.deployed_topology or project.topology or {"nodes": [], "edges": []}
    )

    if project.state == "active" and quiesce_cluster:
        _quiesce_ocp_cluster(host, project_id, topology, pattern_id)

    disk_nodes, vm_nodes, _, vm_to_disks = _build_disk_to_vm_map(topology)

    # Skip already-captured disks (resume after partial failure)
    existing_disks = {
        pd.source_disk_id
        for pd in db.query(PatternDisk).filter_by(pattern_id=pattern_id).all()
    }
    if existing_disks:
        log.info(
            "Pattern %s: %d disk(s) already captured, skipping",
            pattern_id[:8],
            len(existing_disks),
        )
        disk_nodes = [d for d in disk_nodes if d["id"] not in existing_disks]

    pool = None
    if host.storage_pool_id:
        from app.models.storage_pool import StoragePool

        pool = db.query(StoragePool).filter_by(id=host.storage_pool_id).first()

    creds = _get_s3_config()

    # Capture VM disks via NBD (pattern buffer) or direct (on-host)
    if worker_host:
        success = _capture_via_nbd(
            host,
            worker_host,
            vm_to_disks,
            vm_nodes,
            project_id,
            pattern_id,
            creds,
            pool,
            pattern,
            db,
        )
    else:
        success = _capture_direct(
            host,
            vm_to_disks,
            vm_nodes,
            project_id,
            pattern_id,
            creds,
            pool,
            pattern,
            db,
        )
    if not success:
        return False

    # Capture container images
    if not _capture_container_images(host, topology, pattern_id, creds, pattern, db):
        return False

    # Finalize: update topology, recert, save metadata, notify
    _finalize_pattern_capture(pattern, pattern_id, worker_host, host, db)
    return True


def _mark_capture_error(db, pattern_id):
    """Mark pattern as error and notify frontend. Best-effort, swallows exceptions."""
    try:
        pattern = db.query(Pattern).filter_by(id=pattern_id).first()
        if pattern:
            pattern.state = "error"
            db.commit()
            _set_capture_progress(
                pattern_id,
                {
                    "step": "error",
                    "detail": "Capture failed",
                    "vms": [],
                },
            )
            from app.services.ws_pubsub import notify_pattern

            notify_pattern(pattern_id, {"type": "capture-complete", "state": "error"})
    except Exception:
        pass


def capture_pattern_disks(
    pattern_id: str,
    project_id: str,
    restart_after: bool = True,
    quiesce_cluster: bool = True,
) -> None:
    """Capture all disks from a project into a pattern.

    Runs in a background thread, spawned by the patterns API when creating from a source project.
    Uploads each disk to S3 via troshkad on the host, creates PatternDisk records, and updates pattern state.
    """
    from app.models.host import Host
    from app.models.project import Project

    db = SessionLocal()
    try:
        pattern = db.query(Pattern).filter_by(id=pattern_id).first()
        project = db.query(Project).filter_by(id=project_id).first()
        if not pattern or not project:
            log.error("Pattern or project not found: %s / %s", pattern_id, project_id)
            return

        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host:
            pattern.state = "error"
            db.commit()
            log.error("No host found for project %s", project_id)
            return

        if host.host_type == "kubevirt-cluster":
            _capture_kubevirt_native(db, pattern, project, host, restart_after)
            return

        worker_host = _get_pattern_buffer(db, host)

        _run_capture_pipeline(
            db,
            pattern,
            host,
            worker_host,
            project,
            project_id,
            pattern_id,
            quiesce_cluster,
        )

    except Exception as e:
        log.exception("Pattern capture failed for %s: %s", pattern_id, e)
        _mark_capture_error(db, pattern_id)
    finally:
        import time

        time.sleep(2)
        _clear_capture_progress(pattern_id)
        db.close()
