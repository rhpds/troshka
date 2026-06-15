"""Service for provisioning and managing pattern buffer worker hosts."""
import logging
import threading
import uuid

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
    from app.services.agent_deployer import deploy_agent
    from app.services.provisioner import provision_host

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
                pool.ca_cert,
                pool.ca_key,
                result["public_ip"],
                result.get("private_ip", ""),
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
        logger.exception(
            "Failed to provision pattern buffer for pool %s: %s", pool_id, e
        )
    finally:
        db.close()


def replace_pattern_buffer(db: Session, pool: StoragePool):
    """Terminate existing pattern buffer and provision a new one."""
    if pool.worker_host_id:
        old_host = db.query(Host).filter_by(id=pool.worker_host_id).first()
        if old_host and old_host.instance_id:
            from app.services.provisioner import terminate_host

            try:
                credentials = None
                if pool.provider:
                    credentials = pool.provider.get_credentials()
                terminate_host(old_host.instance_id, credentials=credentials)
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
