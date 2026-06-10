import logging
import threading

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


@router.get("/", response_model=list[StoragePoolResponse])
def list_pools(user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pools = db.query(StoragePool).order_by(StoragePool.created_at).all()
    results = []
    for pool in pools:
        resp = StoragePoolResponse.model_validate(pool)
        resp.host_count = db.query(Host).filter(Host.storage_pool_id == pool.id).count()
        results.append(resp)
    return results


@router.get("/{pool_id}", response_model=StoragePoolResponse)
def get_pool(pool_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")
    resp = StoragePoolResponse.model_validate(pool)
    resp.host_count = db.query(Host).filter(Host.storage_pool_id == pool.id).count()
    return resp


@router.post("/", response_model=StoragePoolResponse, status_code=201)
def create_pool(body: StoragePoolCreate, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if body.mode not in ("local", "shared-fsx", "shared-byo"):
        raise HTTPException(400, f"Invalid mode: {body.mode}")

    existing = db.query(StoragePool).filter(StoragePool.name == body.name).first()
    if existing:
        raise HTTPException(409, f"Pool named '{body.name}' already exists")

    provider = db.query(Provider).get(body.provider_id)
    if not provider:
        raise HTTPException(404, "Provider not found")

    if body.mode == "shared-fsx":
        if not body.az:
            raise HTTPException(400, "AZ is required for shared-fsx pools")
        if not body.fsx_throughput_mbps or not body.fsx_storage_gb:
            raise HTTPException(400, "fsx_throughput_mbps and fsx_storage_gb are required")

    if body.mode == "shared-byo":
        if not body.nfs_endpoint:
            raise HTTPException(400, "nfs_endpoint is required for shared-byo pools")

    pool = StoragePool(
        name=body.name,
        mode=body.mode,
        az=body.az,
        nfs_endpoint=body.nfs_endpoint,
        fsx_throughput_mbps=body.fsx_throughput_mbps,
        fsx_storage_gb=body.fsx_storage_gb,
        status="available" if body.mode in ("local", "shared-byo") else "creating",
        provider_id=body.provider_id,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)

    if body.mode == "shared-fsx":
        credentials = provider.get_credentials()
        region = provider.default_region

        subnet_id = storage_pool_service.ensure_subnet_in_az(
            credentials, region, provider.vpc_id, body.az
        )
        pool.subnet_id = subnet_id
        db.commit()

        storage_pool_service.add_sg_rules_for_shared_storage(
            credentials, region, provider.security_group_id
        )

        t = threading.Thread(
            target=storage_pool_service.provision_fsx_pool,
            args=(pool.id, credentials, region, subnet_id,
                  provider.security_group_id, body.fsx_storage_gb, body.fsx_throughput_mbps),
            daemon=True,
        )
        t.start()

    elif body.mode == "shared-byo":
        pass

    resp = StoragePoolResponse.model_validate(pool)
    resp.host_count = 0
    return resp


@router.patch("/{pool_id}", response_model=StoragePoolResponse)
def update_pool(pool_id: str, body: StoragePoolUpdate,
                user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")

    if pool.mode == "shared-fsx":
        provider = db.query(Provider).get(pool.provider_id)
        credentials = provider.get_credentials()

        if body.fsx_throughput_mbps and body.fsx_throughput_mbps != pool.fsx_throughput_mbps:
            storage_pool_service.update_fsx_throughput(
                credentials, provider.default_region, pool.fsx_filesystem_id, body.fsx_throughput_mbps
            )
            pool.fsx_throughput_mbps = body.fsx_throughput_mbps

        if body.fsx_storage_gb and body.fsx_storage_gb > (pool.fsx_storage_gb or 0):
            storage_pool_service.update_fsx_storage(
                credentials, provider.default_region, pool.fsx_filesystem_id, body.fsx_storage_gb
            )
            pool.fsx_storage_gb = body.fsx_storage_gb

    if pool.mode == "shared-byo":
        if body.nfs_endpoint is not None:
            pool.nfs_endpoint = body.nfs_endpoint

    db.commit()
    db.refresh(pool)
    resp = StoragePoolResponse.model_validate(pool)
    resp.host_count = db.query(Host).filter(Host.storage_pool_id == pool.id).count()
    return resp


@router.delete("/{pool_id}", status_code=204)
def delete_pool(pool_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")

    host_count = db.query(Host).filter(Host.storage_pool_id == pool.id).count()
    if host_count > 0:
        raise HTTPException(400, f"Pool still has {host_count} hosts assigned")

    if pool.mode == "shared-fsx" and pool.fsx_filesystem_id:
        provider = db.query(Provider).get(pool.provider_id)
        credentials = provider.get_credentials()
        storage_pool_service.delete_fsx_filesystem(
            credentials, provider.default_region, pool.fsx_filesystem_id
        )

    db.delete(pool)
    db.commit()


@router.get("/{pool_id}/cache", response_model=list[SharedCacheEntryResponse])
def list_cache(pool_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")
    entries = db.query(SharedCacheEntry).filter(
        SharedCacheEntry.storage_pool_id == pool_id
    ).order_by(SharedCacheEntry.created_at.desc()).all()
    return [SharedCacheEntryResponse.model_validate(e) for e in entries]


@router.delete("/{pool_id}/cache/{entry_id}", status_code=204)
def evict_cache_entry(pool_id: str, entry_id: str,
                      user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    entry = db.query(SharedCacheEntry).filter(
        SharedCacheEntry.id == entry_id,
        SharedCacheEntry.storage_pool_id == pool_id,
    ).first()
    if not entry:
        raise HTTPException(404, "Cache entry not found")
    db.delete(entry)
    db.commit()


@router.post("/{pool_id}/probe-azs", response_model=AzProbeResponse)
def probe_azs(pool_id: str, instance_types: list[str],
              user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if pool:
        provider = db.query(Provider).get(pool.provider_id)
    else:
        raise HTTPException(404, "Storage pool not found")

    credentials = provider.get_credentials()
    az_results = storage_pool_service.probe_az_capacity(
        credentials, provider.default_region, instance_types
    )

    results = []
    for az, data in sorted(az_results.items()):
        results.append(AzProbeResult(
            az=az,
            supported_types=data["supported"],
            unsupported_types=data["unsupported"],
        ))

    recommended = storage_pool_service.find_best_az(az_results, instance_types)
    return AzProbeResponse(results=results, recommended_az=recommended)


@router.post("/{pool_id}/gc")
def run_pool_gc(pool_id: str, dry_run: bool = False,
                user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")
    if pool.mode == "local":
        raise HTTPException(400, "GC only applies to shared storage pools")

    from app.services.gc_service import reconcile_pool
    result = reconcile_pool(pool_id, dry_run=dry_run)
    return result
