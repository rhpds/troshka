"""
Pattern service — captures VM disk snapshots to S3 for pattern storage.
"""

import logging
import os

from app.core.database import SessionLocal
from app.models.pattern import Pattern, PatternDisk

log = logging.getLogger(__name__)

_capture_progress: dict[str, dict] = {}


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
    return _capture_progress.get(pattern_id)


def cancel_capture(pattern_id: str, db) -> None:
    """Cancel in-flight capture jobs on the host."""
    from app.models.host import Host
    from app.services.troshkad_client import TroshkadError, cancel_job

    progress = _capture_progress.pop(pattern_id, None)
    if not progress:
        return
    host_id = progress.get("_host_id")
    job_ids = progress.get("_job_ids", [])
    if not host_id or not job_ids:
        return
    host = db.query(Host).filter_by(id=host_id).first()
    if not host:
        return
    for job_id in job_ids:
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


def capture_pattern_disks(
    pattern_id: str, project_id: str, restart_after: bool = True
) -> None:
    """Capture all disks from a project into a pattern.

    Runs in a background thread, spawned by the patterns API when creating from a source project.
    Uploads each disk to S3 via troshkad on the host, creates PatternDisk records, and updates pattern state.
    """
    from app.models.host import Host
    from app.models.project import Project
    from app.services import s3_storage
    from app.services.troshkad_client import TroshkadError, poll_job, start_job

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

        worker_host = _get_pattern_buffer(db, host)

        topology = (
            project.deployed_topology or project.topology or {"nodes": [], "edges": []}
        )
        disk_nodes = [
            n for n in topology.get("nodes", []) if n.get("type") == "storageNode"
        ]
        vm_nodes = {
            n["id"]: n for n in topology.get("nodes", []) if n.get("type") == "vmNode"
        }

        edges = topology.get("edges", [])
        disk_to_vm = {}
        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            if src in vm_nodes and tgt in [d["id"] for d in disk_nodes]:
                disk_to_vm[tgt] = src
            elif tgt in vm_nodes and src in [d["id"] for d in disk_nodes]:
                disk_to_vm[src] = tgt

        # Group disks by VM so we can make one troshkad call per VM
        vm_to_disks = {}
        for disk_node in disk_nodes:
            vm_id = disk_to_vm.get(disk_node["id"])
            if not vm_id:
                continue
            if vm_id not in vm_to_disks:
                vm_to_disks[vm_id] = []
            vm_to_disks[vm_id].append(disk_node)

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

        total = len(disk_nodes)
        processed = 0

        # Get storage pool for correct disk paths
        pool = None
        if host.storage_pool_id:
            from app.models.storage_pool import StoragePool

            pool = db.query(StoragePool).filter_by(id=host.storage_pool_id).first()

        from app.services.deploy_service import _disk_path
        from app.services.s3_storage import _get_s3_config

        creds = _get_s3_config()

        if worker_host:
            # --- NBD path: offload flatten+upload to pattern buffer ---
            log.info(
                "Pattern %s: using pattern buffer %s for NBD capture",
                pattern_id[:8],
                worker_host.id[:8],
            )
            import time as _time

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
                    cache_path = f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{disk_id}.{fmt}"
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
                vm_name = (
                    vm_nodes.get(vm_id, {}).get("data", {}).get("label", vm_id[:8])
                )
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

            vm_count = len(vm_tasks)
            vm_status = {t["vm_id"]: "waiting" for t in vm_tasks}
            _capture_progress[pattern_id] = {
                "step": "capturing",
                "detail": f"0/{vm_count} VMs done (NBD)",
                "_host_id": host.id,
                "_job_ids": [],
            }

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
                _capture_progress[pattern_id] = progress
                from app.services.ws_pubsub import notify_pattern

                notify_pattern(pattern_id, {"type": "capture-progress", **progress})

            import threading as _threading

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
                log.error(
                    "NBD capture errors for pattern %s: %s", pattern_id[:8], errors
                )
                pattern.state = "error"
                db.commit()
                return

            for task in vm_tasks:
                vid = task["vm_id"]
                nbd_results = results_map.get(vid, [])
                for j, metadata in enumerate(task["disk_metadata"]):
                    size_bytes = (
                        nbd_results[j].get("size_bytes", 0)
                        if j < len(nbd_results)
                        else 0
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

        else:
            # --- Direct path: capture on VM host (original flow) ---
            # Build all capture jobs upfront
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
                    cache_path = f"/var/lib/troshka/local/cache/patterns/{pattern_id}/{disk_id}.{fmt}"

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
                            "virtual_size_bytes": int(
                                disk_node.get("data", {}).get("size", 0)
                            )
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
                        },
                    )
                    vm_name = (
                        vm_nodes.get(vm_id, {}).get("data", {}).get("label", vm_id[:8])
                    )
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
                    log.error(
                        "Failed to start capture for pattern %s VM %s: %s",
                        pattern_id[:8],
                        vm_id[:8],
                        e,
                    )
                    pattern.state = "error"
                    db.commit()
                    return

            # Poll all jobs concurrently, update progress with per-VM status
            import time as _time

            completed_jobs = set()
            deadline = _time.time() + 3600
            _capture_progress[pattern_id] = {
                "step": "capturing",
                "detail": f"0/{len(all_jobs)} VMs done",
                "_host_id": host.id,
                "_job_ids": [j["job_id"] for j in all_jobs],
            }

            while len(completed_jobs) < len(all_jobs) and _time.time() < deadline:
                if pattern_id not in _capture_progress:
                    log.info(
                        "Pattern %s: capture cancelled, exiting poll loop",
                        pattern_id[:8],
                    )
                    return
                lines = []
                for idx, jinfo in enumerate(all_jobs):
                    if jinfo["job_id"] in completed_jobs:
                        lines.append(f"{jinfo['vm_name']}: done")
                        continue
                    try:
                        job = poll_job(host, jinfo["job_id"])
                    except TroshkadError:
                        lines.append(f"{jinfo['vm_name']}: polling...")
                        continue
                    if job["status"] in ("completed", "failed", "cancelled"):
                        completed_jobs.add(jinfo["job_id"])
                        jinfo["_result"] = job
                        if job["status"] in ("failed", "cancelled"):
                            lines.append(f"{jinfo['vm_name']}: {job['status'].upper()}")
                        else:
                            lines.append(f"{jinfo['vm_name']}: done")
                    else:
                        output = job.get("output", [])
                        last = ""
                        for line in reversed(output):
                            if (
                                "Flatten" in line
                                or "Upload" in line
                                or "Commit" in line
                                or "Snapshot" in line
                                or "Trim" in line
                                or "Cach" in line
                            ):
                                last = line
                                break
                        lines.append(
                            f"{jinfo['vm_name']}: {last}"
                            if last
                            else f"{jinfo['vm_name']}: working..."
                        )
                progress = {
                    "step": "capturing",
                    "detail": f"{len(completed_jobs)}/{len(all_jobs)} VMs done",
                    "vms": lines,
                }
                _capture_progress[pattern_id] = progress
                from app.services.ws_pubsub import notify_pattern

                notify_pattern(pattern_id, {"type": "capture-progress", **progress})
                _time.sleep(5)

            # Process results
            for jinfo in all_jobs:
                job = jinfo.get("_result")
                if not job:
                    try:
                        job = poll_job(host, jinfo["job_id"])
                    except TroshkadError:
                        job = {"status": "failed", "result": {"error": "Job lost"}}
                try:
                    if job["status"] == "failed":
                        error_msg = job.get("result", {}).get(
                            "error", "Pattern capture failed"
                        )
                        log.error(
                            "Failed to capture pattern %s VM %s: %s",
                            pattern_id[:8],
                            jinfo["vm_id"][:8],
                            error_msg,
                        )
                        pattern.state = "error"
                        db.commit()
                        return

                    disk_results = job.get("result", {}).get("disks", [])
                    for j, metadata in enumerate(jinfo["disk_metadata"]):
                        size_bytes = (
                            disk_results[j].get("size_bytes", 0)
                            if j < len(disk_results)
                            else 0
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
                    log.info(
                        "Pattern %s: VM %s capture done",
                        pattern_id[:8],
                        jinfo["vm_id"][:8],
                    )

                except TroshkadError as e:
                    log.error(
                        "Troshkad error capturing pattern %s VM %s: %s",
                        pattern_id[:8],
                        jinfo["vm_id"][:8],
                        str(e),
                    )
                    pattern.state = "error"
                    db.commit()
                    return

        # Update pattern topology: point storage nodes to captured pattern disks
        topo = pattern.topology or {}
        disk_map = {d.source_disk_id: d for d in pattern.disks}
        for node in topo.get("nodes", []):
            if node.get("type") != "storageNode":
                continue
            if node.get("data", {}).get("format") == "iso":
                continue
            pd = disk_map.get(node["id"])
            if pd:
                node["data"]["source"] = "pattern"
                node["data"]["patternId"] = pattern_id
                node["data"]["patternDiskId"] = pd.id
                node["data"].pop("libraryItemId", None)
        import copy
        import json

        from sqlalchemy import text

        db.execute(
            text("UPDATE patterns SET topology = :topo WHERE id = :pid"),
            {"topo": json.dumps(copy.deepcopy(topo)), "pid": pattern_id},
        )

        pattern.state = "available"
        pattern.total_size_bytes = sum(d.size_bytes for d in pattern.disks)
        db.commit()

        # Save metadata to S3 for recovery after DB loss
        from app.services import s3_storage

        metadata = {
            "type": "pattern",
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
                Body=json.dumps(metadata),
                ContentType="application/json",
            )
        except Exception:
            log.warning("Failed to save pattern metadata to S3 for %s", pattern_id[:8])

        log.info("Pattern %s capture complete", pattern_id)
        _capture_progress[pattern_id] = {
            "step": "complete",
            "detail": "Capture complete",
            "vms": [],
        }
        from app.services.ws_pubsub import notify_pattern

        notify_pattern(pattern_id, {"type": "capture-complete", "state": "available"})

    except Exception as e:
        log.exception("Pattern capture failed for %s: %s", pattern_id, e)
        try:
            pattern = db.query(Pattern).filter_by(id=pattern_id).first()
            if pattern:
                pattern.state = "error"
                db.commit()
                _capture_progress[pattern_id] = {
                    "step": "error",
                    "detail": "Capture failed",
                    "vms": [],
                }
                from app.services.ws_pubsub import notify_pattern

                notify_pattern(
                    pattern_id, {"type": "capture-complete", "state": "error"}
                )
        except Exception:
            pass
    finally:
        import time

        time.sleep(2)
        _capture_progress.pop(pattern_id, None)
        db.close()
