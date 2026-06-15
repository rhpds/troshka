# Pattern Buffer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Offload flatten/compress/S3 upload from VM hosts to a dedicated "pattern buffer" worker instance per storage pool, using TLS-secured NBD for remote disk reads.

**Architecture:** VM host snapshots the VM (sub-second), exports the frozen base disk via `qemu-nbd` with TLS. The pattern buffer connects over NBD, flattens+compresses locally on fast NVMe, uploads to S3, and seeds the shared cache. VM host only serves read-only blocks — near zero CPU impact after snapshot.

**Tech Stack:** Python (SQLAlchemy, FastAPI, troshkad), qemu-nbd/qemu-img (NBD + TLS), AWS EC2/boto3, React/PatternFly (admin UI)

---

### Task 1: Database Migration — StoragePool worker columns

**Files:**
- Create: `src/backend/alembic/versions/<auto>_add_pattern_buffer_columns.py`
- Modify: `src/backend/app/models/storage_pool.py`

- [ ] **Step 1: Add columns to StoragePool model**

In `src/backend/app/models/storage_pool.py`, add after `provider_id` (line 47):

```python
worker_host_id: Mapped[str | None] = mapped_column(
    ForeignKey("hosts.id", ondelete="SET NULL"), nullable=True
)
worker_instance_type: Mapped[str | None] = mapped_column(
    String(50), default="c6id.xlarge"
)
```

- [ ] **Step 2: Generate Alembic migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic revision -m "add pattern buffer columns to storage pools"
```

Edit the generated file — the `upgrade()` should be:

```python
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.add_column("storage_pools", sa.Column("worker_host_id", sa.String(36), sa.ForeignKey("hosts.id", ondelete="SET NULL"), nullable=True))
    op.add_column("storage_pools", sa.Column("worker_instance_type", sa.String(50), nullable=True, server_default="c6id.xlarge"))

def downgrade():
    op.drop_column("storage_pools", "worker_instance_type")
    op.drop_column("storage_pools", "worker_host_id")
```

- [ ] **Step 3: Run migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/models/storage_pool.py src/backend/alembic/versions/*pattern_buffer*
git commit -m "feat: add worker_host_id and worker_instance_type to StoragePool model"
```

---

### Task 2: Exclude pattern buffer hosts from VM placement

**Files:**
- Modify: `src/backend/app/services/placement.py:98-105`
- Modify: `src/backend/tests/` (if placement tests exist)

- [ ] **Step 1: Add host_type filter to find_available_host**

In `src/backend/app/services/placement.py`, modify `find_available_host()` at line 98:

```python
def find_available_host(
    db: Session,
    required_vcpus: int,
    required_ram_mb: int,
    required_eips: int = 0,
    storage_pool_id: str | None = None,
) -> Host | None:
    """Find the least-loaded active host with enough free capacity (with overcommit).
    Syncs capacity from DB first to handle concurrent deployments."""
    query = db.query(Host).filter(
        Host.state == "active",
        Host.agent_status == "connected",
        Host.host_type != "pattern_buffer",
    )
    if storage_pool_id:
        query = query.filter(Host.storage_pool_id == storage_pool_id)

    hosts = query.all()
```

- [ ] **Step 2: Commit**

```bash
git add src/backend/app/services/placement.py
git commit -m "fix: exclude pattern_buffer hosts from VM placement"
```

---

### Task 3: Troshkad — NBD export endpoint

**Files:**
- Modify: `src/troshkad/troshkad.py` (add near end, before `if __name__`)

- [ ] **Step 1: Add NBD port allocator and export handler**

Add before the `if __name__ == "__main__":` line in `src/troshkad/troshkad.py`:

```python
# ── NBD export for pattern buffer ──

_nbd_ports_lock = threading.Lock()
_nbd_ports = {}  # port -> {"pid": int, "domain": str, "disk_path": str}

NBD_PORT_START = 10809
NBD_PORT_END = 10829


def _allocate_nbd_port():
    """Find the next free port in the NBD range."""
    with _nbd_ports_lock:
        for port in range(NBD_PORT_START, NBD_PORT_END + 1):
            if port not in _nbd_ports:
                return port
    raise RuntimeError("No free NBD ports available")


def _handle_nbd_export(job, params):
    """Snapshot a VM disk and serve it read-only over TLS-secured NBD."""
    domain_name = params.get("domain_name", "")
    disk_path = _validate_path(params.get("disk_path", ""))

    if not domain_name:
        raise RuntimeError("domain_name is required")
    if not os.path.exists(disk_path):
        raise RuntimeError(f"Disk not found: {disk_path}")

    running = _is_domain_running(domain_name)
    snapshotted = False
    if running:
        snapshotted = _snapshot_domain(job, domain_name)

    port = _allocate_nbd_port()
    tls_dir = "/etc/pki/libvirt"

    cmd = [
        "qemu-nbd",
        "--read-only",
        "--port", str(port),
        "--export-name", "disk",
        "--persistent",
        "--fork",
    ]
    if os.path.exists(os.path.join(tls_dir, "servercert.pem")):
        cmd.extend(["--tls-creds", f"dir={tls_dir},endpoint=server"])

    if running and not snapshotted:
        cmd.append("-U")

    cmd.append(disk_path)

    _job_log(job, f"Starting NBD export on port {port}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        if snapshotted:
            _commit_snapshot(job, domain_name)
        raise RuntimeError(f"qemu-nbd failed: {result.stderr.strip()}")

    # Find the qemu-nbd PID
    pid = None
    try:
        ps = subprocess.run(
            ["fuser", f"{port}/tcp"],
            capture_output=True, text=True, timeout=5,
        )
        pid = int(ps.stdout.strip().split()[-1]) if ps.stdout.strip() else None
    except Exception:
        pass

    with _nbd_ports_lock:
        _nbd_ports[port] = {
            "pid": pid,
            "domain": domain_name,
            "disk_path": disk_path,
            "snapshotted": snapshotted,
        }

    _job_log(job, f"NBD export active on port {port} (PID {pid})")
    return {"port": port, "export_name": "disk", "snapshotted": snapshotted}


COMMAND_HANDLERS["nbd/export"] = _handle_nbd_export
```

- [ ] **Step 2: Add NBD stop endpoint**

```python
def _handle_nbd_stop(job, params):
    """Stop NBD export and commit snapshot overlay."""
    domain_name = params.get("domain_name", "")
    port = int(params.get("port", 0))

    if not port:
        raise RuntimeError("port is required")

    with _nbd_ports_lock:
        info = _nbd_ports.pop(port, None)

    if info and info.get("pid"):
        try:
            os.kill(info["pid"], signal.SIGTERM)
            _job_log(job, f"Killed qemu-nbd PID {info['pid']} on port {port}")
        except ProcessLookupError:
            _job_log(job, f"qemu-nbd PID {info['pid']} already exited")
    else:
        subprocess.run(["fuser", "-k", f"{port}/tcp"],
                       capture_output=True, timeout=10)
        _job_log(job, f"Killed process on port {port} via fuser")

    if domain_name and info and info.get("snapshotted"):
        _job_log(job, "Committing snapshot overlay...")
        _commit_snapshot(job, domain_name)

    return {"port": port, "stopped": True}


COMMAND_HANDLERS["nbd/stop"] = _handle_nbd_stop
```

- [ ] **Step 3: Add `signal` import at top of file if not present**

Check for `import signal` near the top of troshkad.py. If missing, add it with the other stdlib imports.

- [ ] **Step 4: Commit**

```bash
git add src/troshkad/troshkad.py
git commit -m "feat(troshkad): add nbd/export and nbd/stop endpoints for pattern buffer"
```

---

### Task 4: Troshkad — NBD pull-flatten endpoint (runs on pattern buffer)

**Files:**
- Modify: `src/troshkad/troshkad.py`

- [ ] **Step 1: Add pull-flatten handler**

Add after the `nbd/stop` handler:

```python
def _handle_nbd_pull_flatten(job, params):
    """Connect to remote NBD export, flatten+compress to local disk."""
    nbd_host = params.get("nbd_host", "")
    nbd_port = int(params.get("nbd_port", 0))
    export_name = params.get("export_name", "disk")
    output_path = _validate_path(params.get("output_path", ""))
    tls_dir = params.get("tls_dir", "/etc/pki/libvirt")

    if not nbd_host or not nbd_port:
        raise RuntimeError("nbd_host and nbd_port are required")
    if not output_path:
        raise RuntimeError("output_path is required")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    nbd_url = f"nbd://{nbd_host}:{nbd_port}/{export_name}"
    cmd = ["qemu-img", "convert"]

    if os.path.exists(os.path.join(tls_dir, "clientcert.pem")):
        cmd.extend([
            "--object",
            f"tls-creds-x509,id=tls0,dir={tls_dir},endpoint=client",
            "--image-opts",
        ])
        nbd_url = (
            f"driver=nbd,host={nbd_host},port={nbd_port},"
            f"export={export_name},tls-creds=tls0"
        )

    cmd.extend(["-c", "-o", "compression_type=zstd", "-O", "qcow2"])
    cmd.append(nbd_url)
    cmd.append(output_path)

    _job_log(job, f"Pulling from {nbd_host}:{nbd_port}, flattening to {os.path.basename(output_path)}")

    flatten_done = threading.Event()

    def _monitor():
        while not flatten_done.is_set():
            try:
                if os.path.exists(output_path):
                    cur = os.path.getsize(output_path)
                    cur_gb = round(cur / (1024**3), 1)
                    _job_log(job, f"Flattening: {cur_gb} GB written")
            except OSError:
                pass
            flatten_done.wait(10)

    mon = threading.Thread(target=_monitor, daemon=True)
    mon.start()

    try:
        _run_cmd(job, cmd, timeout=3600)
    finally:
        flatten_done.set()

    size_bytes = os.path.getsize(output_path)
    size_gb = round(size_bytes / (1024**3), 1)
    _job_log(job, f"Flatten complete: {size_gb} GB")
    return {"size_bytes": size_bytes, "output_path": output_path}


COMMAND_HANDLERS["nbd/pull-flatten"] = _handle_nbd_pull_flatten
```

- [ ] **Step 2: Commit**

```bash
git add src/troshkad/troshkad.py
git commit -m "feat(troshkad): add nbd/pull-flatten endpoint for pattern buffer"
```

---

### Task 5: Add NBD security group rules

**Files:**
- Modify: `src/backend/app/services/storage_pool_service.py:352-401`

- [ ] **Step 1: Add NBD port range to security group rules**

In `add_sg_rules_for_shared_storage()`, add after the migration port rule (line 394):

```python
    if 10809 not in existing_ports:
        rules_to_add.append(
            {
                "IpProtocol": "tcp",
                "FromPort": 10809,
                "ToPort": 10829,
                "UserIdGroupPairs": [{"GroupId": security_group_id}],
            }
        )
```

- [ ] **Step 2: Commit**

```bash
git add src/backend/app/services/storage_pool_service.py
git commit -m "feat: add NBD port range (10809-10829) to pool security group rules"
```

---

### Task 6: Pattern buffer provisioning service

**Files:**
- Create: `src/backend/app/services/pattern_buffer_service.py`

- [ ] **Step 1: Create the service**

```python
"""Service for provisioning and managing pattern buffer worker hosts."""
import logging
import threading

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.host import Host
from app.models.storage_pool import StoragePool

logger = logging.getLogger(__name__)

DEFAULT_INSTANCE_TYPE = "c6id.xlarge"
DEFAULT_STORAGE_GB = 200


def provision_pattern_buffer_async(pool_id: str):
    """Spawn a background thread to provision a pattern buffer for a pool."""
    thread = threading.Thread(
        target=_provision_pattern_buffer, args=(pool_id,), daemon=True
    )
    thread.start()


def _provision_pattern_buffer(pool_id: str):
    """Provision a pattern buffer host for a storage pool."""
    import uuid

    from app.services.provisioner import provision_host
    from app.services.agent_deployer import deploy_agent

    db = SessionLocal()
    try:
        pool = db.query(StoragePool).filter_by(id=pool_id).first()
        if not pool:
            logger.error("Pool %s not found for pattern buffer provisioning", pool_id)
            return
        if pool.worker_host_id:
            existing = db.query(Host).filter_by(id=pool.worker_host_id).first()
            if existing and existing.state == "active":
                logger.info("Pool %s already has an active pattern buffer", pool_id)
                return

        provider = pool.provider
        if not provider:
            logger.error("Pool %s has no provider", pool_id)
            return

        credentials = provider.get_credentials()
        region = provider.region

        instance_type = pool.worker_instance_type or DEFAULT_INSTANCE_TYPE
        host_id = str(uuid.uuid4())

        nfs_kwargs = {}
        if pool.mode == "shared-fsx" and pool.fsx_dns_name:
            nfs_kwargs["nfs_server"] = pool.fsx_mount_ip or pool.fsx_dns_name
            nfs_kwargs["nfs_path"] = "/fsx/"
        elif pool.mode == "shared-byo" and pool.nfs_endpoint:
            parts = pool.nfs_endpoint.split(":")
            nfs_kwargs["nfs_server"] = parts[0]
            nfs_kwargs["nfs_path"] = parts[1] if len(parts) > 1 else "/"

        logger.info(
            "Provisioning pattern buffer for pool %s: %s", pool_id[:8], instance_type
        )

        result = provision_host(
            instance_type=instance_type,
            host_id=host_id,
            region=region,
            credentials=credentials,
            storage_size_gb=DEFAULT_STORAGE_GB,
            subnet_id=pool.subnet_id,
            security_group_id=provider.security_group_id,
            **nfs_kwargs,
        )

        host = Host(
            id=host_id,
            instance_id=result["instance_id"],
            instance_type=result["instance_type"],
            region=region,
            state="active",
            host_type="pattern_buffer",
            total_vcpus=result["total_vcpus"],
            total_ram_mb=result["total_ram_mb"],
            ip_address=result["public_ip"],
            private_ip=result.get("private_ip", ""),
            key_pair_name=result["key_pair_name"],
            private_key=result["private_key"],
            storage_size_gb=result.get("storage_size_gb", DEFAULT_STORAGE_GB),
            storage_pool_id=pool_id,
            provider_id=provider.id,
        )
        db.add(host)
        pool.worker_host_id = host_id
        db.commit()
        db.refresh(host)

        logger.info("Pattern buffer %s provisioned, installing agent...", host_id[:8])

        storage_mode = "shared" if nfs_kwargs else "local"
        cert_pem = key_pem = ca_pem = ""
        if pool.ca_cert and pool.ca_key:
            from app.services.storage_pool_service import sign_host_cert
            cert_pem, key_pem = sign_host_cert(
                pool.ca_cert, pool.ca_key,
                result["public_ip"], result.get("private_ip", ""),
            )
            ca_pem = pool.ca_cert

        deploy_agent(
            host_ip=result["public_ip"],
            private_key=result["private_key"],
            host_id=host_id,
            storage_mode=storage_mode,
            nfs_server=nfs_kwargs.get("nfs_server", ""),
            nfs_path=nfs_kwargs.get("nfs_path", ""),
            ca_cert=ca_pem,
            host_cert=cert_pem,
            host_key=key_pem,
        )

        host.agent_status = "connected"
        db.commit()
        logger.info("Pattern buffer %s ready for pool %s", host_id[:8], pool_id[:8])

    except Exception as e:
        logger.exception("Failed to provision pattern buffer for pool %s: %s", pool_id, e)
    finally:
        db.close()


def replace_pattern_buffer(db: Session, pool: StoragePool):
    """Terminate existing pattern buffer and provision a new one."""
    if pool.worker_host_id:
        old_host = db.query(Host).filter_by(id=pool.worker_host_id).first()
        if old_host:
            from app.services.provisioner import terminate_host
            try:
                terminate_host(old_host)
            except Exception as e:
                logger.warning("Failed to terminate old pattern buffer: %s", e)
            old_host.state = "terminated"

        pool.worker_host_id = None
        db.commit()

    provision_pattern_buffer_async(pool.id)


def get_pattern_buffer_host(db: Session, pool_id: str) -> Host | None:
    """Get the active pattern buffer host for a pool, or None."""
    pool = db.query(StoragePool).filter_by(id=pool_id).first()
    if not pool or not pool.worker_host_id:
        return None
    host = db.query(Host).filter_by(id=pool.worker_host_id).first()
    if host and host.state == "active" and host.agent_status == "connected":
        return host
    return None
```

- [ ] **Step 2: Commit**

```bash
git add src/backend/app/services/pattern_buffer_service.py
git commit -m "feat: add pattern buffer provisioning service"
```

---

### Task 7: Auto-provision pattern buffer on pool creation

**Files:**
- Modify: `src/backend/app/api/storage_pools.py` (pool creation endpoint)

- [ ] **Step 1: Add auto-provisioning hook**

In `src/backend/app/api/storage_pools.py`, find the pool creation endpoint (POST). After the pool is created and SG rules are added (around line 115), add:

```python
        from app.services.pattern_buffer_service import provision_pattern_buffer_async
        provision_pattern_buffer_async(pool.id)
```

This should go after the `db.commit()` that saves the pool, inside the same block where `add_sg_rules_for_shared_storage()` is called.

- [ ] **Step 2: Add replace/add API endpoint**

Add a new endpoint to `storage_pools.py`:

```python
@router.post("/{pool_id}/pattern-buffer")
def provision_or_replace_pattern_buffer(
    pool_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Provision or replace the pattern buffer for a storage pool."""
    pool = db.query(StoragePool).filter_by(id=pool_id).first()
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")

    from app.services.pattern_buffer_service import replace_pattern_buffer
    replace_pattern_buffer(db, pool)

    return {"status": "provisioning", "pool_id": pool_id}
```

- [ ] **Step 3: Add pattern buffer info to pool response**

In the pool detail/list serialization (check `StoragePoolResponse` schema or the response builder), add:

```python
"worker_host_id": pool.worker_host_id,
"worker_instance_type": pool.worker_instance_type,
```

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/api/storage_pools.py
git commit -m "feat: auto-provision pattern buffer on pool creation + replace endpoint"
```

---

### Task 8: Backend orchestration — NBD-based capture flow

**Files:**
- Modify: `src/backend/app/services/pattern_service.py` (the `capture_pattern_disks` function)

This is the core change. The existing function dispatches `patterns/capture-direct` to the VM host. We add an alternative path that splits the work: VM host exports via NBD, pattern buffer pulls+flattens+uploads.

- [ ] **Step 1: Add helper to get pattern buffer for a project**

Add near the top of `pattern_service.py`:

```python
def _get_pattern_buffer(db, host):
    """Get the pattern buffer host for the pool this host belongs to, if any."""
    if not host.storage_pool_id:
        return None
    from app.services.pattern_buffer_service import get_pattern_buffer_host
    return get_pattern_buffer_host(db, host.storage_pool_id)
```

- [ ] **Step 2: Add NBD capture flow function**

Add a new function that handles the NBD-based capture for a single VM's disks:

```python
def _capture_vm_via_nbd(
    host, worker_host, project_id, vm_id, domain_name,
    disks_params, disk_metadata, creds, pattern_id, running, job_log_fn,
):
    """Capture a VM's disks via NBD export (VM host) + pull-flatten (pattern buffer).

    Returns list of {"size_bytes": int} per disk, or raises on failure.
    """
    results = []
    for i, disk_info in enumerate(disks_params):
        disk_path = disk_info["disk_path"]
        s3_url = disk_info["s3_url"]
        cache_path = disk_info["cache_path"]

        # 1. VM host: snapshot + NBD export
        job_log_fn(f"Exporting {os.path.basename(disk_path)} via NBD...")
        export_job_id = start_job(host, "/nbd/export", {
            "domain_name": domain_name,
            "disk_path": disk_path,
        })
        export_job = wait_for_job(host, export_job_id, timeout=120)
        if export_job["status"] != "completed":
            raise RuntimeError(f"NBD export failed: {export_job.get('result', {}).get('error')}")
        nbd_port = export_job["result"]["port"]

        try:
            # 2. Pattern buffer: pull-flatten
            import tempfile as _tf
            output_filename = f"{pattern_id[:8]}-{vm_id[:8]}-{i}.qcow2"
            output_path = f"/var/lib/troshka/local/tmp/{output_filename}"

            job_log_fn(f"Flattening via NBD from {host.private_ip}:{nbd_port}...")
            flatten_job_id = start_job(worker_host, "/nbd/pull-flatten", {
                "nbd_host": host.private_ip,
                "nbd_port": nbd_port,
                "export_name": "disk",
                "output_path": output_path,
            })
            flatten_job = wait_for_job(worker_host, flatten_job_id, timeout=3600, poll_interval=10)
            if flatten_job["status"] != "completed":
                raise RuntimeError(f"Pull-flatten failed: {flatten_job.get('result', {}).get('error')}")
            flat_size = flatten_job["result"].get("size_bytes", 0)

            # 3. Pattern buffer: S3 upload + cache copy
            job_log_fn(f"Uploading {round(flat_size / (1024**3), 1)} GB to S3...")
            upload_job_id = start_job(worker_host, "/patterns/upload-and-cache", {
                "local_path": output_path,
                "s3_url": s3_url,
                "cache_path": cache_path,
                "aws_access_key_id": creds.get("access_key_id", ""),
                "aws_secret_access_key": creds.get("secret_access_key", ""),
                "aws_region": creds.get("region", "us-east-1"),
            })
            upload_job = wait_for_job(worker_host, upload_job_id, timeout=3600, poll_interval=10)
            if upload_job["status"] != "completed":
                raise RuntimeError(f"Upload failed: {upload_job.get('result', {}).get('error')}")

            results.append({"size_bytes": flat_size})

        finally:
            # 4. VM host: stop NBD + commit overlay
            try:
                stop_job_id = start_job(host, "/nbd/stop", {
                    "domain_name": domain_name,
                    "port": nbd_port,
                })
                wait_for_job(host, stop_job_id, timeout=600)
            except TroshkadError as e:
                log.warning("NBD stop failed for %s port %d: %s", domain_name, nbd_port, e)

    return results
```

- [ ] **Step 3: Add upload-and-cache troshkad handler**

In `src/troshkad/troshkad.py`, add a new command handler:

```python
def _handle_upload_and_cache(job, params):
    """Upload a local file to S3 and copy to cache path."""
    local_path = _validate_path(params["local_path"])
    s3_url = params["s3_url"]
    cache_path = _validate_path(params["cache_path"])
    aws_access_key = params.get("aws_access_key_id", "")
    aws_secret_key = params.get("aws_secret_access_key", "")
    aws_region = params.get("aws_region", "us-east-1")

    if not os.path.exists(local_path):
        raise RuntimeError(f"File not found: {local_path}")

    file_size = os.path.getsize(local_path)

    cache_error = [None]
    def _do_cache():
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            shutil.copy(local_path, cache_path)
        except Exception as e:
            cache_error[0] = e

    cache_thread = threading.Thread(target=_do_cache, daemon=True)
    cache_thread.start()

    _job_log(job, f"Uploading {round(file_size / (1024**3), 1)} GB to S3...")
    _s3_upload_with_cache(job, local_path, file_size, s3_url, cache_path,
                          aws_access_key, aws_secret_key, aws_region)

    _job_log(job, "Upload complete, waiting for cache...")
    while cache_thread.is_alive():
        try:
            if os.path.exists(cache_path):
                cached = os.path.getsize(cache_path)
                cached_gb = round(cached / (1024**3), 1)
                total_gb = round(file_size / (1024**3), 1)
                cache_pct = min(100, int(cached * 100 / file_size)) if file_size > 0 else 0
                _job_log(job, f"Caching: {cached_gb} of {total_gb} GB ({cache_pct}%)")
        except OSError:
            pass
        cache_thread.join(timeout=5)

    if cache_error[0]:
        _job_log(job, f"Cache copy failed: {cache_error[0]}")

    try:
        os.unlink(local_path)
    except OSError:
        pass

    return {"size_bytes": file_size, "cached": cache_error[0] is None}


COMMAND_HANDLERS["patterns/upload-and-cache"] = _handle_upload_and_cache
```

- [ ] **Step 4: Modify capture_pattern_disks to use NBD path when available**

In `capture_pattern_disks()` in `pattern_service.py`, after the host lookup (around line 73), add the pattern buffer detection and modify the dispatch logic:

Find the section where jobs are dispatched (around line 163-188). Wrap the existing dispatch in a condition:

```python
        worker_host = _get_pattern_buffer(db, host)

        # ... existing vm_to_disks loop ...

        if worker_host:
            # NBD-based capture via pattern buffer
            log.info("Pattern %s: using pattern buffer %s", pattern_id[:8], worker_host.id[:8])
            for vm_id, disk_ids in vm_to_disks.items():
                # ... build disks_params and disk_metadata same as before ...
                domain_name = f"troshka-{project_id[:8]}-{vm_id[:8]}"
                vm_name = vm_nodes.get(vm_id, {}).get("data", {}).get("label", vm_id[:8])
                running = True  # assume running for NBD path

                def _log_fn(msg, _vid=vm_id[:8]):
                    _capture_progress[pattern_id] = {
                        "step": "capturing",
                        "detail": msg,
                        "vms": [f"{vm_name}: {msg}"],
                    }

                try:
                    results = _capture_vm_via_nbd(
                        host, worker_host, project_id, vm_id, domain_name,
                        disks_params, disk_metadata, creds, pattern_id, running, _log_fn,
                    )
                    for j, metadata in enumerate(disk_metadata):
                        size_bytes = results[j].get("size_bytes", 0) if j < len(results) else 0
                        pd = PatternDisk(...)  # same as existing code
                        db.add(pd)
                    db.commit()
                except Exception as e:
                    log.exception("NBD capture failed for VM %s: %s", vm_id[:8], e)
                    pattern.state = "error"
                    db.commit()
                    return
        else:
            # Existing troshkad-direct capture flow (unchanged)
            ...
```

The exact integration requires careful merging with the existing code. The key principle: if `worker_host` exists, use `_capture_vm_via_nbd()`. Otherwise, fall through to the existing `start_job(host, "/patterns/capture-direct", ...)` path unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/backend/app/services/pattern_service.py src/troshkad/troshkad.py
git commit -m "feat: NBD-based capture flow using pattern buffer worker"
```

---

### Task 9: Admin UI — Pool page pattern buffer status

**Files:**
- Modify: `src/frontend/src/app/admin/hosts/page.tsx` (or storage pools page if separate)

- [ ] **Step 1: Show pattern buffer status on pool cards**

Find the storage pool section in the admin page. Add a status indicator:

```tsx
{pool.worker_host_id ? (
  <Label color="green">Pattern Buffer: connected</Label>
) : (
  <Label color="orange">No Pattern Buffer</Label>
)}
```

- [ ] **Step 2: Add provision/replace button**

```tsx
<Button
  variant="secondary"
  size="sm"
  onClick={() => {
    fetch(`/api/v1/storage-pools/${pool.id}/pattern-buffer`, { method: "POST" });
    // reload pools after a delay
  }}
>
  {pool.worker_host_id ? "Replace Pattern Buffer" : "Add Pattern Buffer"}
</Button>
```

- [ ] **Step 3: Show pattern buffer hosts with distinct badge in host list**

Where hosts are listed, add a badge when `host.host_type === "pattern_buffer"`:

```tsx
{host.host_type === "pattern_buffer" && (
  <Label color="purple" isCompact>pattern buffer</Label>
)}
```

- [ ] **Step 4: Commit**

```bash
git add src/frontend/src/app/admin/hosts/page.tsx
git commit -m "feat: admin UI for pattern buffer status and provisioning"
```

---

### Task 10: Verify end-to-end

- [ ] **Step 1: Verify qemu-nbd is available on hosts**

SSH to a host and check:
```bash
which qemu-nbd
qemu-nbd --version
```

If not available, add `qemu-img` (which includes `qemu-nbd`) to the agent install script.

- [ ] **Step 2: Test NBD export/stop manually**

On a VM host with a test project:
```bash
# Export a disk
qemu-nbd --read-only --port 10809 --export-name disk --persistent --fork /var/lib/troshka/vms/<project>/<disk>.qcow2

# From another host, verify connectivity
qemu-img info nbd://<host-ip>:10809/disk

# Stop
fuser -k 10809/tcp
```

- [ ] **Step 3: Test pattern buffer capture via API**

1. Create a pool with a pattern buffer
2. Deploy a project to the pool
3. Capture a pattern — verify it uses the NBD path in backend logs
4. Verify pattern state becomes "available"
5. Deploy from the captured pattern — verify it works

- [ ] **Step 4: Test fallback**

1. Stop the pattern buffer host's troshkad
2. Capture a pattern — verify it falls back to direct capture on the VM host
3. Verify backend logs show the fallback warning

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: pattern buffer — dedicated storage worker for pool captures"
```
