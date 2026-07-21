"""Service for provisioning and managing pattern buffer worker hosts."""

import logging
import threading
import uuid

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.host import Host
from app.models.provider import Provider
from app.models.storage_pool import StoragePool

logger = logging.getLogger(__name__)

DEFAULT_INSTANCE_TYPE = "i4i.large"
DEFAULT_STORAGE_GB = 200

_provisioning: set[str] = set()
_provision_errors: dict[str, str] = {}


def is_provisioning(pool_id: str) -> bool:
    return pool_id in _provisioning


def get_provision_error(pool_id: str) -> str | None:
    return _provision_errors.get(pool_id)


def _find_ec2_provider(db: Session, pool: StoragePool) -> Provider | None:
    """Find the EC2 provider for a pool by looking at existing hosts in the pool."""
    if pool.provider_id:
        prov = db.query(Provider).filter_by(id=pool.provider_id, type="ec2").first()
        if prov:
            return prov
    host = (
        db.query(Host)
        .filter(Host.storage_pool_id == pool.id, Host.provider_id.isnot(None))
        .first()
    )
    if host and host.provider_id:
        return db.query(Provider).filter_by(id=host.provider_id, type="ec2").first()
    return None


def provision_pattern_buffer_async(pool_id: str):
    """Spawn a background thread to provision a pattern buffer for a pool."""
    thread = threading.Thread(
        target=_provision_pattern_buffer, args=(pool_id,), daemon=True
    )
    thread.start()


def _provision_pattern_buffer(pool_id: str):
    """Provision a pattern buffer host for a storage pool."""
    from app.services.agent_deployer import deploy_agent
    from app.services.providers import get_provider_driver

    _provisioning.add(pool_id)
    _provision_errors.pop(pool_id, None)
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
            logger.error("No provider found for pool %s", pool_id)
            return

        driver = get_provider_driver(provider)
        if pool.worker_instance_type:
            instance_type = pool.worker_instance_type
        elif provider.type == "gcp":
            instance_type = "e2-standard-2"
        elif provider.type == "azure":
            instance_type = "Standard_E2s_v5"
        else:
            instance_type = DEFAULT_INSTANCE_TYPE
        host_id = str(uuid.uuid4())

        nfs_kwargs = {}
        if pool.mode == "shared-fsx" and pool.fsx_dns_name:
            nfs_kwargs["nfs_server"] = pool.fsx_dns_name
            nfs_kwargs["nfs_path"] = "/fsx"
        elif pool.mode == "shared-netapp" and pool.netapp_mount_ip:
            nfs_kwargs["nfs_server"] = pool.netapp_mount_ip
            nfs_kwargs["nfs_path"] = f"/{pool.netapp_volume_name or 'troshka'}"
        elif pool.mode == "shared-azure-files" and pool.azure_file_share_url:
            parts = pool.azure_file_share_url.split(":", 1)
            nfs_kwargs["nfs_server"] = parts[0]
            nfs_kwargs["nfs_path"] = parts[1] if len(parts) > 1 else "/"
        elif pool.mode in ("shared-byo", "shared-ceph-nfs") and pool.nfs_endpoint:
            parts = pool.nfs_endpoint.split(":", 1)
            nfs_kwargs["nfs_server"] = parts[0]
            nfs_kwargs["nfs_path"] = parts[1] if len(parts) > 1 else "/"
            if pool.nfs_port:
                nfs_kwargs["nfs_port"] = pool.nfs_port

        result = None
        logger.info(
            "Provisioning pattern buffer for pool %s: type=%s instance=%s image=%s provider=%s(%s)",
            pool_id[:8],
            provider.type,
            instance_type,
            (provider.default_image or "none")[:40],
            provider.name,
            provider.id[:8],
        )

        result = driver.provision_host(
            provider=provider,
            host_id=host_id,
            instance_type=instance_type,
            storage_size_gb=DEFAULT_STORAGE_GB,
            image_id=provider.default_image,
            region=provider.default_region,
            vpc_id=provider.vpc_id,
            subnet_id=pool.subnet_id or provider.subnet_id,
            security_group_id=provider.security_group_id,
            host_type="pattern_buffer",
            **nfs_kwargs,
        )

        host = Host(
            id=host_id,
            provider_id=provider.id,
            instance_id=result["instance_id"],
            instance_type=result["instance_type"],
            region=provider.default_region,
            state="active",
            host_type="pattern_buffer",
            total_vcpus=result["total_vcpus"],
            total_ram_mb=result["total_ram_mb"],
            ip_address=result["public_ip"],
            private_ip=result.get("private_ip", ""),
            key_pair_name=result.get("key_pair_name"),
            private_key=result.get("private_key"),
            storage_size_gb=result.get("storage_size_gb", DEFAULT_STORAGE_GB),
            max_eips=0,
            storage_pool_id=pool_id,
        )
        db.add(host)
        db.flush()
        pool.worker_host_id = host_id
        from datetime import UTC, datetime

        pool.pb_last_activity_at = datetime.now(UTC)
        db.commit()
        db.refresh(host)

        ssh_port = result.get("_ssh_port", 22)
        ssh_host = result.get("_ssh_host") or result["public_ip"]

        from app.services.agent_deployer import (
            get_provider_data_disk,
            get_provider_ssh_user,
        )

        ssh_user = get_provider_ssh_user(provider.type)
        data_disk = get_provider_data_disk(provider.type)

        logger.info("Pattern buffer %s provisioned, waiting for SSH...", host_id[:8])

        from app.services.agent_deployer import wait_for_ssh

        if not wait_for_ssh(
            ssh_host,
            result["private_key"],
            port=ssh_port,
            ssh_user=ssh_user,
            timeout=300,
        ):
            logger.error("Pattern buffer %s SSH never became available", host_id[:8])
            return

        logger.info("Pattern buffer %s SSH ready, installing agent...", host_id[:8])

        storage_mode = "shared" if nfs_kwargs else "local"
        cert_pem = key_pem = ca_pem = ""
        if pool.ca_cert and pool.ca_key:
            from app.services.storage_pool_service import sign_host_cert

            cert_pem, key_pem = sign_host_cert(
                pool.ca_cert,
                pool.ca_key,
                result["public_ip"],
                result.get("private_ip", ""),
            )
            ca_pem = pool.ca_cert

        from app.services.agent_ca_service import get_agent_ca_cert

        deploy_result = deploy_agent(
            host_ip=ssh_host,
            private_key=result["private_key"],
            host_id=host_id,
            storage_mode=storage_mode,
            nfs_server=nfs_kwargs.get("nfs_server", ""),
            nfs_path=nfs_kwargs.get("nfs_path", ""),
            nfs_port=nfs_kwargs.get("nfs_port", 0),
            ca_cert=ca_pem,
            host_cert=cert_pem,
            host_key=key_pem,
            host_type="pattern_buffer",
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            data_disk_device=data_disk,
            vncd_no_tls=provider.type == "ocpvirt",
            agent_ca_cert=get_agent_ca_cert(),
        )

        creds = deploy_result.get("troshkad_credentials", {})
        if creds.get("token"):
            host.agent_token = creds["token"]
        if creds.get("fingerprint"):
            host.agent_cert_fingerprint = creds["fingerprint"]

        host.agent_status = "connected"
        host.ip_address = result["public_ip"]
        db.commit()
        logger.info(
            "Pattern buffer %s ready for pool %s (public %s, private %s)",
            host_id[:8],
            pool_id[:8],
            host.ip_address,
            host.private_ip,
        )

    except Exception as e:
        logger.exception(
            "Failed to provision pattern buffer for pool %s: %s", pool_id, e
        )
        _provision_errors[
            pool_id
        ] = f"Provisioning failed ({type(e).__name__}). Check server logs for details."
        db.rollback()
        try:
            _result = locals().get("result")
            _provider = locals().get("provider")
            if (
                _result
                and isinstance(_result, dict)
                and _result.get("instance_id")
                and _provider
            ):
                from app.services.providers import get_provider_driver as _get_drv

                _get_drv(_provider).terminate_host(_provider, _result["instance_id"])
                logger.info("Cleaned up orphaned instance %s", _result["instance_id"])
        except Exception:
            logger.warning("Failed to clean up instance after error", exc_info=True)
    finally:
        _provisioning.discard(pool_id)
        db.close()


def _check_pattern_buffer_busy(db: Session, pool: StoragePool) -> str | None:
    """Return a reason string if the pattern buffer is busy, None if idle."""
    if not pool.worker_host_id:
        return None
    host = db.query(Host).filter_by(id=pool.worker_host_id).first()
    if not host or host.agent_status != "connected":
        return None

    from app.services.troshkad_client import check_health

    health = check_health(host)
    if health and health.get("running_jobs", 0) > 0:
        return f"{health['running_jobs']} active job(s) on pattern buffer"

    from app.services.pattern_service import _capture_progress

    for pattern_id, progress in _capture_progress.items():
        if progress.get("step") in ("capturing",):
            return f"Pattern capture in progress ({pattern_id[:8]})"

    return None


def replace_pattern_buffer(db: Session, pool: StoragePool):
    """Terminate existing pattern buffer and provision a new one."""
    busy = _check_pattern_buffer_busy(db, pool)
    if busy:
        raise RuntimeError(f"Cannot replace pattern buffer: {busy}")
    if pool.worker_host_id:
        old_host = db.query(Host).filter_by(id=pool.worker_host_id).first()
        if old_host:
            if old_host.instance_id and pool.provider:
                try:
                    from app.services.providers import get_provider_driver

                    drv = get_provider_driver(pool.provider)
                    drv.terminate_host(pool.provider, old_host.instance_id)
                except Exception as e:
                    logger.warning("Failed to terminate old pattern buffer: %s", e)
            old_host.state = "terminated"
            old_host.agent_status = "disconnected"

        pool.worker_host_id = None
        db.commit()

    provision_pattern_buffer_async(pool.id)


def stop_pattern_buffer(db: Session, pool: StoragePool):
    """Stop the pattern buffer instance (EC2 stop, not terminate)."""
    busy = _check_pattern_buffer_busy(db, pool)
    if busy:
        raise RuntimeError(f"Cannot stop pattern buffer: {busy}")
    if not pool.worker_host_id:
        return
    host = db.query(Host).filter_by(id=pool.worker_host_id).first()
    if not host or not host.instance_id:
        return

    provider = pool.provider
    if not provider:
        return

    from app.services.providers import get_provider_driver

    drv = get_provider_driver(provider)
    drv.stop_host(provider, host.instance_id)

    # For EC2, wait for the instance to actually stop
    if provider.type == "ec2":
        import boto3

        credentials = provider.get_credentials()
        ec2 = boto3.client(
            "ec2",
            region_name=provider.default_region,
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
        )
        waiter = ec2.get_waiter("instance_stopped")
        waiter.wait(InstanceIds=[host.instance_id])

    host.state = "stopped"
    host.agent_status = "disconnected"
    db.commit()
    logger.info("Pattern buffer %s stopped", host.id[:8])


def wake_pattern_buffer(db: Session, pool: StoragePool, timeout: int = 120) -> bool:
    """Start a stopped pattern buffer and wait for the agent to respond.

    Updates the host IP (changes on stop/start) and flushes the connection
    pool cache. Returns True if agent is ready, False on failure.
    """
    if not pool.worker_host_id:
        return False
    host = db.query(Host).filter_by(id=pool.worker_host_id).first()
    if not host or not host.instance_id:
        return False
    if host.state == "active" and host.agent_status == "connected":
        return True

    provider = pool.provider
    if not provider:
        return False

    import time

    from app.services.providers import get_provider_driver

    drv = get_provider_driver(provider)

    logger.info("Waking pattern buffer %s...", host.id[:8])
    drv.start_host(provider, host.instance_id)

    # Wait for running + get new IP
    if provider.type == "ec2":
        import boto3

        credentials = provider.get_credentials()
        ec2 = boto3.client(
            "ec2",
            region_name=provider.default_region,
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
        )
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[host.instance_id])
        desc = ec2.describe_instances(InstanceIds=[host.instance_id])
        inst = desc["Reservations"][0]["Instances"][0]
        new_ip = inst.get("PublicIpAddress", "")
    else:
        # Non-EC2: poll driver for status
        status: dict | None = None
        for _ in range(60):
            status = drv.get_host_status(provider, host.instance_id)
            if status and status["state"] == "running":
                break
            time.sleep(5)
        new_ip = (
            status.get("public_ip") or status.get("private_ip") or "" if status else ""
        )

    logger.info("Pattern buffer %s running", host.id[:8])
    from app.services.troshkad_client import _pools as _connection_pools

    if new_ip and new_ip != host.ip_address:
        logger.info(
            "Pattern buffer %s IP changed: %s -> %s",
            host.id[:8],
            host.ip_address,
            new_ip,
        )
        old_keys = [
            k
            for k in _connection_pools
            if host.ip_address and k.startswith(host.ip_address + ":")
        ]
        for k in old_keys:
            del _connection_pools[k]
        host.ip_address = new_ip

    host.state = "active"
    db.commit()

    from app.services.troshkad_client import check_health

    start = time.time()
    while time.time() - start < timeout:
        result = check_health(host)
        if result:
            from datetime import UTC, datetime

            pool.pb_last_activity_at = datetime.now(UTC)
            host.agent_status = "connected"
            db.commit()
            logger.info(
                "Pattern buffer %s awake (%.0fs)", host.id[:8], time.time() - start
            )
            return True
        time.sleep(3)

    logger.warning("Pattern buffer %s failed to wake after %ds", host.id[:8], timeout)
    return False


def get_pattern_buffer_host(
    db: Session, pool_id: str, auto_wake: bool = True
) -> Host | None:
    """Get the pattern buffer host for a pool. Auto-wakes if stopped."""
    pool = db.query(StoragePool).filter_by(id=pool_id).first()
    if not pool or not pool.worker_host_id:
        return None
    host = db.query(Host).filter_by(id=pool.worker_host_id).first()
    if not host:
        return None
    if host.state == "active" and host.agent_status == "connected":
        return host
    if host.state == "stopped" and auto_wake:
        logger.info(
            "Auto-waking pattern buffer %s for pool %s", host.id[:8], pool_id[:8]
        )
        if wake_pattern_buffer(db, pool):
            return host
    return None


def touch_activity(db, pool_id: str):
    """Record that the pattern buffer for this pool was just used."""
    from datetime import UTC, datetime

    pool = db.query(StoragePool).filter_by(id=pool_id).first()
    if pool:
        pool.pb_last_activity_at = datetime.now(UTC)
        db.commit()


def check_auto_sleep(db):
    """Check all pools for idle pattern buffers and auto-sleep them."""
    from datetime import UTC, datetime

    from app.models.host import Host

    pools = (
        db.query(StoragePool)
        .filter(
            StoragePool.worker_host_id.isnot(None),
            StoragePool.pb_auto_sleep_minutes > 0,
        )
        .all()
    )

    for pool in pools:
        host = db.query(Host).filter_by(id=pool.worker_host_id).first()
        if not host or host.state != "active" or host.agent_status != "connected":
            continue

        last_activity = pool.pb_last_activity_at
        if last_activity is None:
            pool.pb_last_activity_at = datetime.now(UTC)
            db.commit()
            continue

        idle_seconds = (datetime.now(UTC) - last_activity).total_seconds()
        threshold_seconds = pool.pb_auto_sleep_minutes * 60

        if idle_seconds < threshold_seconds:
            continue

        busy = _check_pattern_buffer_busy(db, pool)
        if busy:
            logger.debug(
                "Pool %s PB idle %.0fs but busy: %s", pool.name, idle_seconds, busy
            )
            continue

        logger.info(
            "Auto-sleeping pattern buffer for pool %s (idle %.0fm, threshold %dm)",
            pool.name,
            idle_seconds / 60,
            pool.pb_auto_sleep_minutes,
        )
        try:
            stop_pattern_buffer(db, pool)
        except Exception:
            logger.warning("Auto-sleep failed for pool %s", pool.name, exc_info=True)
