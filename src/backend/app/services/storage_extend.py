# src/backend/app/services/storage_extend.py
"""Auto-extend and manual extend for FSx (pool) and EBS (host) storage."""
import logging
import time

logger = logging.getLogger(__name__)

_last_extend: dict[str, float] = {}
_COOLDOWN_SECONDS = 600


def _on_cooldown(target_id: str) -> bool:
    last = _last_extend.get(target_id, 0)
    return (time.time() - last) < _COOLDOWN_SECONDS


def _mark_extended(target_id: str):
    _last_extend[target_id] = time.time()


def should_extend_host(host) -> bool:
    if not host.auto_extend_enabled:
        return False
    if host.auto_extend_max_gb and host.storage_size_gb >= host.auto_extend_max_gb:
        return False
    if _on_cooldown(f"host:{host.id}"):
        return False
    warnings = host.storage_warnings or []
    data_mounts = ["/var/lib/troshka", "/var/lib/troshka/local"]
    for w in warnings:
        if (
            w["mount"] in data_mounts
            and w["used_pct"] >= host.auto_extend_threshold_pct
        ):
            return True
    return False


def should_extend_pool(pool, current_used_pct: float) -> bool:
    if pool.mode not in ("shared-fsx", "shared-netapp", "shared-azure-files"):
        return False
    if not pool.auto_extend_enabled:
        return False
    current_gb = (
        pool.fsx_storage_gb
        or pool.netapp_capacity_gb
        or pool.azure_files_capacity_gb
        or 0
    )
    if pool.auto_extend_max_gb and current_gb >= pool.auto_extend_max_gb:
        return False
    if _on_cooldown(f"pool:{pool.id}"):
        return False
    return current_used_pct >= pool.auto_extend_threshold_pct


def extend_host_ebs(host, db, increment_gb: int | None = None):
    """Extend a host's EBS data volume. Returns new size or raises."""
    increment = increment_gb or host.auto_extend_increment_gb
    new_size = host.storage_size_gb + increment

    if host.auto_extend_max_gb:
        new_size = min(new_size, host.auto_extend_max_gb)
    if new_size <= host.storage_size_gb:
        raise ValueError(f"Cannot extend: already at max ({host.storage_size_gb} GB)")

    provider = host.provider
    if not provider:
        raise ValueError("No provider associated with host")
    creds = provider.get_credentials()

    from app.services.provisioner import _get_ec2_client

    ec2 = _get_ec2_client(credentials=creds)

    volumes = ec2.describe_volumes(
        Filters=[
            {"Name": "attachment.instance-id", "Values": [host.instance_id]},
            {"Name": "attachment.device", "Values": ["/dev/sdf", "/dev/xvdf"]},
        ]
    )
    if not volumes["Volumes"]:
        raise ValueError("No data volume found on instance")

    vol_id = volumes["Volumes"][0]["VolumeId"]
    old_size = host.storage_size_gb

    ec2.modify_volume(VolumeId=vol_id, Size=new_size)
    logger.info(
        "Extended EBS volume %s from %d to %d GB for host %s",
        vol_id,
        old_size,
        new_size,
        host.id[:8],
    )

    if host.agent_status == "connected":
        from app.services.troshkad_client import start_job, wait_for_job

        job_id = start_job(host, "/host/resize-storage", {})
        wait_for_job(host, job_id, timeout=30)

    host.storage_size_gb = new_size
    db.commit()
    _mark_extended(f"host:{host.id}")
    return {"old_size_gb": old_size, "new_size_gb": new_size, "volume_id": vol_id}


def extend_pool_fsx(pool, db, increment_gb: int | None = None):
    """Extend an FSx filesystem. Returns new size or raises."""
    increment = increment_gb or pool.auto_extend_increment_gb
    new_size = (pool.fsx_storage_gb or 0) + increment

    if pool.auto_extend_max_gb:
        new_size = min(new_size, pool.auto_extend_max_gb)
    if new_size <= (pool.fsx_storage_gb or 0):
        raise ValueError(f"Cannot extend: already at max ({pool.fsx_storage_gb} GB)")

    import math

    min_grow = math.ceil((pool.fsx_storage_gb or 64) * 1.1)
    if new_size < min_grow:
        new_size = min_grow

    from app.models.provider import Provider

    provider = db.query(Provider).get(pool.provider_id)
    if not provider:
        raise ValueError("No provider associated with pool")
    creds = provider.get_credentials()

    from app.services.storage_pool_service import update_fsx_storage

    old_size = pool.fsx_storage_gb or 0
    try:
        update_fsx_storage(
            creds, provider.default_region, pool.fsx_filesystem_id, new_size
        )
    except Exception as e:
        logger.error("FSx extend failed for pool %s: %s", pool.id[:8], e)
        msg = str(e)
        if "6 hours" in msg or "prior storage capacity" in msg:
            raise ValueError(
                "FSx storage can only be extended once every 6 hours. Try again later."
            )
        raise ValueError("FSx extend failed. Check backend logs for details.")

    pool.fsx_storage_gb = new_size
    db.commit()
    _mark_extended(f"pool:{pool.id}")
    logger.info(
        "Extended FSx %s from %d to %d GB for pool %s",
        pool.fsx_filesystem_id,
        old_size,
        new_size,
        pool.name,
    )
    return {
        "old_size_gb": old_size,
        "new_size_gb": new_size,
        "filesystem_id": pool.fsx_filesystem_id,
    }


def extend_pool_netapp(pool, db, increment_gb: int | None = None):
    """Extend a GCP NetApp Volumes pool. Returns new size or raises."""
    increment = increment_gb or pool.auto_extend_increment_gb
    new_size = (pool.netapp_capacity_gb or 0) + increment

    if pool.auto_extend_max_gb:
        new_size = min(new_size, pool.auto_extend_max_gb)
    if new_size <= (pool.netapp_capacity_gb or 0):
        raise ValueError(
            f"Cannot extend: already at max ({pool.netapp_capacity_gb} GB)"
        )

    from app.models.provider import Provider

    provider = db.query(Provider).get(pool.provider_id)
    if not provider:
        raise ValueError("No provider associated with pool")
    creds = provider.get_credentials()

    from app.services.storage_pool_service import update_netapp_capacity

    old_size = pool.netapp_capacity_gb or 0
    update_netapp_capacity(creds, pool.netapp_pool_id, new_size)

    pool.netapp_capacity_gb = new_size
    db.commit()
    _mark_extended(f"pool:{pool.id}")
    logger.info(
        "Extended NetApp pool %s from %d to %d GB for pool %s",
        pool.netapp_pool_id,
        old_size,
        new_size,
        pool.name,
    )
    return {
        "old_size_gb": old_size,
        "new_size_gb": new_size,
        "netapp_pool_id": pool.netapp_pool_id,
    }


def extend_pool_azure_files(pool, db, increment_gb: int | None = None):
    """Extend an Azure Files NFS share. Returns new size or raises."""
    increment = increment_gb or pool.auto_extend_increment_gb
    new_size = (pool.azure_files_capacity_gb or 0) + increment

    if pool.auto_extend_max_gb:
        new_size = min(new_size, pool.auto_extend_max_gb)
    if new_size <= (pool.azure_files_capacity_gb or 0):
        raise ValueError(
            f"Cannot extend: already at max ({pool.azure_files_capacity_gb} GB)"
        )

    from app.models.provider import Provider

    provider = db.query(Provider).get(pool.provider_id)
    if not provider:
        raise ValueError("No provider associated with pool")
    creds = provider.get_credentials()

    from app.services.storage_pool_service import update_azure_files_capacity

    old_size = pool.azure_files_capacity_gb or 0
    update_azure_files_capacity(
        creds,
        provider.azure_resource_group,
        pool.azure_storage_account,
        pool.azure_file_share_name,
        new_size,
    )

    pool.azure_files_capacity_gb = new_size
    db.commit()
    _mark_extended(f"pool:{pool.id}")
    logger.info(
        "Extended Azure Files share %s/%s from %d to %d GB for pool %s",
        pool.azure_storage_account,
        pool.azure_file_share_name,
        old_size,
        new_size,
        pool.name,
    )
    return {
        "old_size_gb": old_size,
        "new_size_gb": new_size,
        "storage_account": pool.azure_storage_account,
    }
