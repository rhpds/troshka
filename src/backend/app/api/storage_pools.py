import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.host import Host
from app.models.provider import Provider
from app.models.storage_pool import SharedCacheEntry, StoragePool
from app.models.user import User
from app.schemas.storage_pool import (
    AzProbeResponse,
    AzProbeResult,
    SharedCacheEntryResponse,
    StoragePoolCreate,
    StoragePoolResponse,
    StoragePoolUpdate,
)
from app.services import storage_pool_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/storage-pools", tags=["storage-pools"])

_ERR_PROVIDER_NOT_FOUND = "Provider not found"
_ERR_POOL_NOT_FOUND = "Storage pool not found"
_ERR_PROVIDER_MISSING_REGION = "Provider missing default region"


def _pool_response(pool: StoragePool, db: Session) -> StoragePoolResponse:
    resp = StoragePoolResponse.model_validate(pool)
    resp.host_count = (
        db.query(Host)
        .filter(Host.storage_pool_id == pool.id, Host.host_type != "pattern_buffer")
        .count()
    )
    if pool.worker_host_id:
        worker = db.query(Host).filter_by(id=pool.worker_host_id).first()
        if worker:
            resp.worker_ip = worker.ip_address
            resp.worker_private_ip = worker.private_ip
            resp.worker_instance_id = worker.instance_id
            resp.worker_agent_version = worker.agent_version
            if worker.agent_status == "connected":
                resp.worker_status = "connected"
            elif worker.state == "active":
                resp.worker_status = "installing"
            else:
                resp.worker_status = worker.state
        else:
            resp.worker_status = "error"
    elif pool.worker_instance_type:
        from app.services.pattern_buffer_service import (
            get_provision_error,
            is_provisioning,
        )

        if is_provisioning(pool.id):
            resp.worker_status = "provisioning"
        else:
            err = get_provision_error(pool.id)
            if err:
                resp.worker_status = "error"
                resp.worker_error = err
    return resp


@router.get("/", response_model=list[StoragePoolResponse])
def list_pools(
    user: User = Depends(require_role("admin")), db: Session = Depends(get_db)
):
    pools = db.query(StoragePool).order_by(StoragePool.created_at).all()
    return [_pool_response(pool, db) for pool in pools]


@router.get(
    "/{pool_id}",
    response_model=StoragePoolResponse,
    responses={404: {"description": _ERR_POOL_NOT_FOUND}},
)
def get_pool(
    pool_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    pool = db.get(StoragePool, pool_id)
    if not pool:
        raise HTTPException(404, _ERR_POOL_NOT_FOUND)
    return _pool_response(pool, db)


_VALID_POOL_MODES = (
    "local",
    "shared-fsx",
    "shared-byo",
    "shared-ceph-nfs",
    "shared-netapp",
    "shared-azure-files",
)


def _validate_pool_mode(body: StoragePoolCreate, provider: Provider) -> None:
    """Validate mode-specific required fields; raises HTTPException on error."""
    if body.mode == "shared-fsx":
        if not body.az:
            raise HTTPException(400, "AZ is required for shared-fsx pools")
        if not body.fsx_throughput_mbps or not body.fsx_storage_gb:
            raise HTTPException(
                400, "fsx_throughput_mbps and fsx_storage_gb are required"
            )
    elif body.mode == "shared-byo":
        if not body.nfs_endpoint:
            raise HTTPException(400, "nfs_endpoint is required for shared-byo pools")
    elif body.mode == "shared-ceph-nfs":
        if provider.type != "ocpvirt":
            raise HTTPException(400, "Ceph-NFS pools require an OCP Virt provider")
    elif body.mode == "shared-netapp":
        if provider.type != "gcp":
            raise HTTPException(400, "NetApp Volumes pools require a GCP provider")
        if not body.netapp_capacity_gb:
            raise HTTPException(400, "netapp_capacity_gb is required")
    elif body.mode == "shared-azure-files":
        if provider.type != "azure":
            raise HTTPException(400, "Azure Files pools require an Azure provider")
        if not body.azure_files_capacity_gb:
            raise HTTPException(400, "azure_files_capacity_gb is required")


def _provision_fsx(
    pool: StoragePool, body: StoragePoolCreate, provider: Provider, db: Session
) -> None:
    credentials = provider.get_credentials()
    region = provider.default_region
    sg_id = provider.security_group_id
    if not region or not sg_id:
        raise HTTPException(400, "Provider missing region or security group")

    subnet_id = storage_pool_service.ensure_subnet_in_az(
        credentials, region, provider.vpc_id, body.az  # type: ignore[arg-type]
    )
    pool.subnet_id = subnet_id
    db.commit()

    storage_pool_service.add_sg_rules_for_shared_storage(credentials, region, sg_id)

    from app.core.redis import enqueue_job

    enqueue_job(
        storage_pool_service.provision_fsx_pool,
        pool.id,
        credentials,
        region,
        subnet_id,
        sg_id,
        body.fsx_storage_gb,
        body.fsx_throughput_mbps,
        queue_name="host_lifecycle",
    )


def _provision_byo(provider: Provider) -> None:
    credentials = provider.get_credentials()
    region = provider.default_region
    sg_id = provider.security_group_id
    if not region or not sg_id:
        raise HTTPException(400, "Provider missing region or security group")
    storage_pool_service.add_sg_rules_for_shared_storage(
        credentials, region, sg_id, include_nfs=False
    )


def _provision_ceph_nfs(pool: StoragePool, provider: Provider) -> None:
    credentials = provider.get_credentials()
    from app.core.redis import enqueue_job

    enqueue_job(
        storage_pool_service.provision_ceph_nfs_pool,
        pool.id,
        credentials,
        queue_name="host_lifecycle",
    )


def _provision_netapp(
    pool: StoragePool, body: StoragePoolCreate, provider: Provider
) -> None:
    credentials = provider.get_credentials()
    region = provider.default_region or "us-central1"
    network = provider.gcp_network_id
    if not network:
        raise HTTPException(400, "GCP provider has no network configured")
    from app.core.redis import enqueue_job

    enqueue_job(
        storage_pool_service.provision_netapp_pool,
        pool.id,
        credentials,
        provider.gcp_project_id,
        region,
        network,
        body.netapp_capacity_gb,
        "troshka",
        body.netapp_service_level or "FLEX",
        queue_name="host_lifecycle",
    )


def _provision_azure_files(
    pool: StoragePool, body: StoragePoolCreate, provider: Provider
) -> None:
    credentials = provider.get_credentials()
    location = provider.azure_location or provider.default_region
    subnet_id = provider.azure_subnet_id
    if not subnet_id:
        raise HTTPException(400, "Azure provider has no subnet configured")
    from app.core.redis import enqueue_job

    enqueue_job(
        storage_pool_service.provision_azure_files_pool,
        pool.id,
        credentials,
        provider.azure_resource_group,
        location,
        subnet_id,
        body.azure_files_capacity_gb,
        body.azure_files_iops,
        body.azure_files_throughput,
        queue_name="host_lifecycle",
    )


def _provision_pool_storage(
    pool: StoragePool, body: StoragePoolCreate, provider: Provider, db: Session
) -> None:
    """Kick off mode-specific storage provisioning."""
    if body.mode == "shared-fsx":
        _provision_fsx(pool, body, provider, db)
    elif body.mode == "shared-byo":
        _provision_byo(provider)
    elif body.mode == "shared-ceph-nfs":
        _provision_ceph_nfs(pool, provider)
    elif body.mode == "shared-netapp":
        _provision_netapp(pool, body, provider)
    elif body.mode == "shared-azure-files":
        _provision_azure_files(pool, body, provider)


@router.post(
    "/",
    response_model=StoragePoolResponse,
    status_code=201,
    responses={404: {"description": _ERR_PROVIDER_NOT_FOUND}},
)
def create_pool(
    body: StoragePoolCreate,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    if body.mode not in _VALID_POOL_MODES:
        raise HTTPException(400, f"Invalid mode: {body.mode}")

    existing = db.query(StoragePool).filter(StoragePool.name == body.name).first()
    if existing:
        raise HTTPException(409, f"Pool named '{body.name}' already exists")

    provider = db.get(Provider, body.provider_id)
    if not provider:
        raise HTTPException(404, _ERR_PROVIDER_NOT_FOUND)

    _validate_pool_mode(body, provider)

    ca_cert, ca_key = None, None
    if body.mode.startswith("shared"):
        ca_cert, ca_key = storage_pool_service.generate_pool_ca(body.name)

    pool = StoragePool(
        name=body.name,
        mode=body.mode,
        az=body.az,
        nfs_endpoint=body.nfs_endpoint,
        fsx_throughput_mbps=body.fsx_throughput_mbps,
        fsx_storage_gb=body.fsx_storage_gb,
        ca_cert=ca_cert,
        ca_key=ca_key,
        status="available" if body.mode in ("local", "shared-byo") else "creating",
        provider_id=body.provider_id,
        worker_instance_type="4c-8g" if body.mode == "shared-ceph-nfs" else None,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)

    _provision_pool_storage(pool, body, provider, db)

    return _pool_response(pool, db)


def _update_fsx_pool(pool: StoragePool, body: StoragePoolUpdate, db: Session) -> None:
    """Apply FSx throughput / storage changes to the pool."""
    provider = db.get(Provider, pool.provider_id)
    if not provider:
        raise HTTPException(404, _ERR_PROVIDER_NOT_FOUND)
    credentials = provider.get_credentials()
    region = provider.default_region
    if not region:
        raise HTTPException(400, _ERR_PROVIDER_MISSING_REGION)
    fsx_id = pool.fsx_filesystem_id
    if not fsx_id:
        raise HTTPException(400, "Pool missing FSx filesystem ID")

    if (
        body.fsx_throughput_mbps
        and body.fsx_throughput_mbps != pool.fsx_throughput_mbps
    ):
        storage_pool_service.update_fsx_throughput(
            credentials, region, fsx_id, body.fsx_throughput_mbps
        )
        pool.fsx_throughput_mbps = body.fsx_throughput_mbps

    if body.fsx_storage_gb and body.fsx_storage_gb > (pool.fsx_storage_gb or 0):
        import math

        min_grow = math.ceil((pool.fsx_storage_gb or 64) * 1.1)
        if body.fsx_storage_gb < min_grow:
            raise HTTPException(
                400,
                f"Storage increase must be at least 10% (minimum {min_grow} GB)",
            )
        storage_pool_service.update_fsx_storage(
            credentials, region, fsx_id, body.fsx_storage_gb
        )
        pool.fsx_storage_gb = body.fsx_storage_gb


def _apply_auto_extend_fields(pool: StoragePool, body: StoragePoolUpdate) -> None:
    """Copy auto-extend and pattern-buffer settings from the update body."""
    if body.auto_extend_enabled is not None:
        pool.auto_extend_enabled = body.auto_extend_enabled
    if body.auto_extend_threshold_pct is not None:
        pool.auto_extend_threshold_pct = body.auto_extend_threshold_pct
    if body.auto_extend_increment_gb is not None:
        pool.auto_extend_increment_gb = body.auto_extend_increment_gb
    if body.auto_extend_max_gb is not None:
        pool.auto_extend_max_gb = body.auto_extend_max_gb
    if body.pb_auto_sleep_minutes is not None:
        pool.pb_auto_sleep_minutes = body.pb_auto_sleep_minutes


@router.patch(
    "/{pool_id}",
    response_model=StoragePoolResponse,
    responses={404: {"description": _ERR_POOL_NOT_FOUND}},
)
def update_pool(
    pool_id: str,
    body: StoragePoolUpdate,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    pool = db.get(StoragePool, pool_id)
    if not pool:
        raise HTTPException(404, _ERR_POOL_NOT_FOUND)

    if pool.mode == "shared-fsx":
        _update_fsx_pool(pool, body, db)

    if pool.mode == "shared-byo":
        if body.nfs_endpoint is not None:
            pool.nfs_endpoint = body.nfs_endpoint

    _apply_auto_extend_fields(pool, body)

    db.commit()
    db.refresh(pool)
    return _pool_response(pool, db)


@router.post(
    "/{pool_id}/extend",
    responses={404: {"description": _ERR_POOL_NOT_FOUND}},
)
def extend_pool(
    pool_id: str,
    body: dict | None = None,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Extend the FSx filesystem by the configured increment."""
    pool = db.get(StoragePool, pool_id)
    if not pool:
        raise HTTPException(404, _ERR_POOL_NOT_FOUND)
    if pool.mode != "shared-fsx":
        raise HTTPException(400, "Only FSx pools can be extended")

    increment_gb = (body or {}).get("increment_gb")
    from app.services.storage_extend import extend_pool_fsx

    try:
        result = extend_pool_fsx(pool, db, increment_gb=increment_gb)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


def _terminate_pattern_buffer(pool: StoragePool, db: Session) -> None:
    """Terminate the pattern buffer instance and clear the association."""
    if not pool.worker_host_id:
        return
    worker = db.query(Host).filter_by(id=pool.worker_host_id).first()
    if worker and worker.state not in ("terminated",):
        try:
            from app.services.providers import get_provider_driver

            provider = db.get(Provider, pool.provider_id)
            drv = get_provider_driver(provider)
            drv.terminate_host(provider, worker.instance_id)
            logger.info("Terminated pattern buffer %s", worker.id[:8])
        except Exception:
            logger.warning(
                "Failed to terminate pattern buffer %s",
                worker.id[:8],
                exc_info=True,
            )
        worker.state = "terminated"
        worker.agent_status = "disconnected"
    pool.worker_host_id = None
    db.commit()


def _cleanup_pool_storage(pool: StoragePool, db: Session) -> None:
    """Delete the backing storage (FSx filesystem, Ceph subvolume, etc.)."""
    if pool.mode == "shared-fsx" and pool.fsx_filesystem_id:
        provider = db.get(Provider, pool.provider_id)
        if not provider:
            raise HTTPException(404, _ERR_PROVIDER_NOT_FOUND)
        credentials = provider.get_credentials()
        region = provider.default_region
        if not region:
            raise HTTPException(400, _ERR_PROVIDER_MISSING_REGION)
        storage_pool_service.delete_fsx_filesystem(
            credentials, region, pool.fsx_filesystem_id
        )

    if pool.mode == "shared-ceph-nfs":
        provider = db.get(Provider, pool.provider_id)
        if not provider:
            raise HTTPException(404, _ERR_PROVIDER_NOT_FOUND)
        credentials = provider.get_credentials()
        storage_pool_service.delete_ceph_nfs_pool(
            pool.id, credentials, pool.ceph_subvolume_group
        )


@router.delete(
    "/{pool_id}",
    status_code=204,
    responses={404: {"description": _ERR_POOL_NOT_FOUND}},
)
def delete_pool(
    pool_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    pool = db.get(StoragePool, pool_id)
    if not pool:
        raise HTTPException(404, _ERR_POOL_NOT_FOUND)

    host_count = (
        db.query(Host)
        .filter(
            Host.storage_pool_id == pool.id,
            Host.host_type != "pattern_buffer",
        )
        .count()
    )
    if host_count > 0:
        raise HTTPException(400, f"Pool still has {host_count} hosts assigned")

    _terminate_pattern_buffer(pool, db)
    _cleanup_pool_storage(pool, db)

    db.delete(pool)
    db.commit()


@router.get(
    "/{pool_id}/cache",
    response_model=list[SharedCacheEntryResponse],
    responses={404: {"description": _ERR_POOL_NOT_FOUND}},
)
def list_cache(
    pool_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    pool = db.get(StoragePool, pool_id)
    if not pool:
        raise HTTPException(404, _ERR_POOL_NOT_FOUND)
    entries = (
        db.query(SharedCacheEntry)
        .filter(SharedCacheEntry.storage_pool_id == pool_id)
        .order_by(SharedCacheEntry.created_at.desc())
        .all()
    )
    return [SharedCacheEntryResponse.model_validate(e) for e in entries]


@router.delete("/{pool_id}/cache/{entry_id}", status_code=204)
def evict_cache_entry(
    pool_id: str,
    entry_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    entry = (
        db.query(SharedCacheEntry)
        .filter(
            SharedCacheEntry.id == entry_id,
            SharedCacheEntry.storage_pool_id == pool_id,
        )
        .first()
    )
    if not entry:
        raise HTTPException(404, "Cache entry not found")
    db.delete(entry)
    db.commit()


@router.post(
    "/{pool_id}/probe-azs",
    response_model=AzProbeResponse,
    responses={404: {"description": _ERR_POOL_NOT_FOUND}},
)
def probe_azs(
    pool_id: str,
    instance_types: list[str],
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    pool = db.get(StoragePool, pool_id)
    if pool:
        provider = db.get(Provider, pool.provider_id)
    else:
        raise HTTPException(404, _ERR_POOL_NOT_FOUND)

    if not provider:
        raise HTTPException(404, _ERR_PROVIDER_NOT_FOUND)
    credentials = provider.get_credentials()
    region = provider.default_region
    if not region:
        raise HTTPException(400, _ERR_PROVIDER_MISSING_REGION)
    az_results = storage_pool_service.probe_az_capacity(
        credentials, region, instance_types
    )

    results = []
    for az, data in sorted(az_results.items()):
        results.append(
            AzProbeResult(
                az=az,
                supported_types=data["supported"],
                unsupported_types=data["unsupported"],
            )
        )

    recommended = storage_pool_service.find_best_az(az_results, instance_types)
    return AzProbeResponse(results=results, recommended_az=recommended)


@router.post(
    "/{pool_id}/gc",
    responses={404: {"description": _ERR_POOL_NOT_FOUND}},
)
def run_pool_gc(
    pool_id: str,
    dry_run: bool = False,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    pool = db.get(StoragePool, pool_id)
    if not pool:
        raise HTTPException(404, _ERR_POOL_NOT_FOUND)
    if pool.mode == "local":
        raise HTTPException(400, "GC only applies to shared storage pools")

    from app.services.gc_service import reconcile_pool

    result = reconcile_pool(pool_id, dry_run=dry_run)
    return result


@router.post("/{pool_id}/pattern-buffer")
def provision_or_replace_pattern_buffer(
    pool_id: str,
    body: dict | None = None,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Provision or replace the pattern buffer for a storage pool."""
    pool = db.query(StoragePool).filter_by(id=pool_id).first()
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")

    if body and body.get("instance_type"):
        pool.worker_instance_type = body["instance_type"]
        db.commit()

    from app.services.pattern_buffer_service import replace_pattern_buffer

    try:
        replace_pattern_buffer(db, pool)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "provisioning", "pool_id": pool_id}


@router.delete("/{pool_id}/pattern-buffer")
def delete_pattern_buffer(
    pool_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Terminate and remove the pattern buffer for a storage pool."""
    pool = db.query(StoragePool).filter_by(id=pool_id).first()
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    if not pool.worker_host_id:
        raise HTTPException(status_code=404, detail="No pattern buffer to delete")

    worker = db.query(Host).filter_by(id=pool.worker_host_id).first()
    if worker and worker.state not in ("terminated",):
        try:
            from app.services.providers import get_provider_driver

            provider = db.get(Provider, pool.provider_id)
            drv = get_provider_driver(provider)
            drv.terminate_host(provider, worker.instance_id)
        except Exception:
            logger.warning("Failed to terminate PB %s", worker.id[:8], exc_info=True)
        worker.state = "terminated"
        worker.agent_status = "disconnected"

    pool.worker_host_id = None
    db.commit()
    return {"status": "deleted", "pool_id": pool_id}


@router.post("/{pool_id}/pattern-buffer/stop")
def stop_pool_pattern_buffer(
    pool_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Stop (sleep) the pattern buffer for a storage pool."""
    pool = db.query(StoragePool).filter_by(id=pool_id).first()
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")

    from app.services.pattern_buffer_service import stop_pattern_buffer

    try:
        stop_pattern_buffer(db, pool)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "stopped", "pool_id": pool_id}


@router.post("/{pool_id}/pattern-buffer/wake")
def wake_pool_pattern_buffer(
    pool_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Wake the pattern buffer for a storage pool."""
    pool = db.query(StoragePool).filter_by(id=pool_id).first()
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")

    from app.services.pattern_buffer_service import wake_pattern_buffer

    success = wake_pattern_buffer(db, pool)
    if not success:
        raise HTTPException(status_code=503, detail="Pattern buffer failed to wake")
    return {"status": "connected", "pool_id": pool_id}
