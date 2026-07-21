"""Background health check poller for troshkad hosts.

Periodically calls GET /health on each connected host to:
- Update last_health_at timestamp
- Sync capacity (vcpus, ram, storage) from live host data
- Update agent_version
- Detect disconnected hosts (mark as disconnected after timeout)
- Auto-reconnect hosts that come back online
"""

import logging
import threading
import time
from datetime import UTC, datetime

from app.core.config import config

logger = logging.getLogger(__name__)

# Read intervals from config with sensible defaults
_health_config = getattr(config, "health", None)
_INTERVAL_SECONDS = (
    getattr(_health_config, "interval_seconds", 30) if _health_config else 30
)
_DISCONNECT_AFTER_SECONDS = (
    getattr(_health_config, "disconnect_after_seconds", 180) if _health_config else 180
)

_WARNING_PCT = 85
_CRITICAL_PCT = 95


def _evaluate_partitions(health):
    """Check partition usage and return warnings list, or None if all healthy."""
    partitions = health.get("partitions", [])
    if not partitions:
        return None
    SKIP_MOUNTS = {"/mnt/iso", "/boot", "/boot/efi"}
    warnings = []
    for p in partitions:
        mount = p.get("mount", "")
        if mount in SKIP_MOUNTS or p.get("fstype") == "iso9660":
            continue
        pct = p.get("used_pct", 0)
        if pct >= _CRITICAL_PCT:
            warnings.append({"mount": p["mount"], "used_pct": pct, "level": "critical"})
        elif pct >= _WARNING_PCT:
            warnings.append({"mount": p["mount"], "used_pct": pct, "level": "warning"})
    return warnings if warnings else None


def _get_initial_ip():
    try:
        from app.services.provisioner import get_public_ip

        return get_public_ip()
    except Exception:
        return None


_last_known_ip = _get_initial_ip()


def _check_ip_change_if_all_unreachable(hosts_checked, hosts_failed):
    """If all hosts failed health check, maybe our IP changed."""
    global _last_known_ip
    if hosts_failed == 0 or hosts_failed < hosts_checked:
        return  # Some hosts are reachable, not an IP issue

    from app.services.provisioner import get_public_ip, update_sg_troshkad_ip

    current_ip = get_public_ip()
    if not current_ip:
        return
    if current_ip == _last_known_ip:
        return

    logger.warning(
        "Public IP changed from %s to %s — updating security groups",
        _last_known_ip,
        current_ip,
    )
    _last_known_ip = current_ip

    # Update all provider SGs
    from app.core.database import SessionLocal
    from app.models.provider import Provider

    db = SessionLocal()
    try:
        providers = (
            db.query(Provider).filter(Provider.security_group_id.isnot(None)).all()
        )
        for provider in providers:
            try:
                creds = provider.get_credentials()
                assert provider.security_group_id is not None
                update_sg_troshkad_ip(
                    provider.security_group_id, current_ip, credentials=creds
                )
            except Exception:
                logger.warning("Failed to update SG for provider %s", provider.id[:8])
    finally:
        db.close()


_skip_until: dict[str, float] = {}


def _poll_hosts():
    """Single poll cycle — check all active hosts."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.services.troshkad_client import check_health

    db = SessionLocal()
    hosts_checked = 0
    hosts_failed = 0
    try:
        # Query hosts that should be polled:
        # - state="active" (not stopped/terminated)
        # - have agent_token (troshkad is installed)
        hosts = (
            db.query(Host)
            .filter(
                Host.state == "active",
                Host.agent_token.isnot(None),
            )
            .all()
        )

        _checked_pools = set()
        now = time.time()
        for host in hosts:
            # KubeVirt native: check K8s API reachability instead of troshkad
            if host.host_type == "kubevirt-cluster":
                try:
                    from app.models.provider import Provider
                    from app.services.providers import get_provider_driver

                    provider = db.query(Provider).filter_by(id=host.provider_id).first()
                    if provider:
                        driver = get_provider_driver(provider)
                        status = driver.get_host_status(provider, host.instance_id)
                        if status:
                            host.agent_status = "connected"
                            host.last_health_at = datetime.now(UTC)
                        else:
                            host.agent_status = "disconnected"
                    db.commit()
                except Exception:
                    host.agent_status = "disconnected"
                    db.commit()
                continue

            if not host.agent_cert_fingerprint:
                continue
            skip_ts = _skip_until.get(host.id)
            if skip_ts and now < skip_ts:
                continue
            hosts_checked += 1
            try:
                health = check_health(host)
                now_dt = datetime.now(UTC)

                if health:
                    # Success — update health data
                    host.last_health_at = now_dt
                    _skip_until.pop(host.id, None)
                    if health.get("version"):
                        host.agent_version = health["version"]

                    # Sync capacity from live data
                    capacity = health.get("capacity", {})
                    if capacity.get("vcpus_total"):
                        host.total_vcpus = capacity["vcpus_total"]
                    if "vcpus_used" in capacity:
                        host.used_vcpus = capacity["vcpus_used"]
                    if capacity.get("ram_total_mb"):
                        host.total_ram_mb = capacity["ram_total_mb"]
                    if "ram_used_mb" in capacity:
                        host.used_ram_mb = capacity["ram_used_mb"]

                    host.storage_warnings = _evaluate_partitions(health)
                    if host.storage_warnings:
                        try:
                            from app.services.storage_extend import should_extend_host

                            if should_extend_host(host) and host.provider:
                                from app.services.providers import get_provider_driver

                                drv = get_provider_driver(host.provider)
                                logger.info(
                                    "Auto-extending storage for host %s",
                                    host.id[:8],
                                )
                                drv.extend_host_storage(host.provider, host, db)
                        except Exception:
                            logger.warning(
                                "Auto-extend failed for host %s",
                                host.id[:8],
                                exc_info=True,
                            )

                    if (
                        host.storage_pool_id
                        and host.storage_pool_id not in _checked_pools
                    ):
                        _checked_pools.add(host.storage_pool_id)
                        pool = host.storage_pool
                        if pool and pool.mode == "shared-fsx":
                            partitions = health.get("partitions", [])
                            shared_mount = next(
                                (
                                    p
                                    for p in partitions
                                    if "shared" in p.get("mount", "")
                                ),
                                None,
                            )
                            if shared_mount:
                                try:
                                    from app.services.storage_extend import (
                                        extend_pool_fsx,
                                        should_extend_pool,
                                    )

                                    if should_extend_pool(
                                        pool, shared_mount["used_pct"]
                                    ):
                                        logger.info(
                                            "Auto-extending FSx for pool %s", pool.name
                                        )
                                        extend_pool_fsx(pool, db)
                                except Exception:
                                    logger.warning(
                                        "Auto-extend failed for pool %s",
                                        pool.name,
                                        exc_info=True,
                                    )

                    # Auto-reconnect if was disconnected or install_failed
                    if host.agent_status in ("disconnected", "install_failed"):
                        host.agent_status = "connected"
                        logger.info(
                            "Host %s reconnected (troshkad %s)",
                            host.id[:8],
                            health.get("version"),
                        )
                        from app.services.gc_service import recover_host_services

                        threading.Thread(
                            target=recover_host_services,
                            args=(host.id,),
                            daemon=True,
                            name=f"recover-{host.id[:8]}",
                        ).start()
                else:
                    hosts_failed += 1
                    if host.agent_status == "connected" and host.last_health_at:
                        elapsed = (now_dt - host.last_health_at).total_seconds()
                        if elapsed > _DISCONNECT_AFTER_SECONDS:
                            host.agent_status = "disconnected"
                            _skip_until[host.id] = time.time() + 30
                            logger.warning(
                                "Host %s marked disconnected (no health for %ds, retrying in 30s)",
                                host.id[:8],
                                int(elapsed),
                            )
                        else:
                            _skip_until[host.id] = time.time() + 15
                    elif host.agent_status == "disconnected":
                        if (
                            host.host_type == "pattern_buffer"
                            and host.last_health_at
                            and (now_dt - host.last_health_at).total_seconds() > 600
                        ):
                            host.state = "stopped"
                            _skip_until[host.id] = time.time() + 86400
                            logger.info(
                                "Host %s (pattern buffer) auto-stopped after 10min disconnect",
                                host.id[:8],
                            )
                        else:
                            _skip_until[host.id] = time.time() + 30
                    else:
                        _skip_until[host.id] = time.time() + 60
            except Exception:
                hosts_failed += 1
                logger.debug(
                    "Health check failed for host %s", host.id[:8], exc_info=True
                )

        db.commit()

        # Auto-sleep idle pattern buffers
        try:
            from app.services.pattern_buffer_service import check_auto_sleep

            check_auto_sleep(db)
        except Exception:
            logger.debug("Auto-sleep check failed", exc_info=True)

        # Check if all hosts failed — maybe our IP changed
        _check_ip_change_if_all_unreachable(hosts_checked, hosts_failed)
    except Exception:
        logger.exception("Health poller cycle failed")
    finally:
        db.close()


_last_cert_check = 0
_CERT_CHECK_INTERVAL = 3600


def _check_cert_renewal():
    """Check and renew libvirt TLS certs for hosts in shared pools (runs hourly)."""
    global _last_cert_check
    now = time.time()
    if now - _last_cert_check < _CERT_CHECK_INTERVAL:
        return
    _last_cert_check = now

    import base64

    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.storage_pool import StoragePool
    from app.services.storage_pool_service import generate_pool_ca, sign_host_cert
    from app.services.troshkad_client import start_job, wait_for_job

    db = SessionLocal()
    try:
        pools = (
            db.query(StoragePool)
            .filter(
                StoragePool.mode.in_(["shared-fsx", "shared-byo"]),
                StoragePool.ca_cert.isnot(None),
            )
            .all()
        )

        for pool in pools:
            if not pool.ca_cert or not pool.ca_key:
                continue
            # Check CA expiry — renew if within 90 days
            try:
                from cryptography import x509

                ca = x509.load_pem_x509_certificate(pool.ca_cert.encode())
                days_left = (ca.not_valid_after_utc - datetime.now(UTC)).days
                if days_left < 90:
                    logger.info(
                        "Pool %s CA expires in %d days, regenerating",
                        pool.name,
                        days_left,
                    )
                    new_cert, new_key = generate_pool_ca(pool.name)
                    pool.ca_cert = new_cert
                    pool.ca_key = new_key
                    db.commit()
            except Exception:
                logger.debug(
                    "CA expiry check failed for pool %s", pool.name, exc_info=True
                )
            hosts = (
                db.query(Host)
                .filter(
                    Host.storage_pool_id == pool.id,
                    Host.state == "active",
                    Host.agent_status == "connected",
                )
                .all()
            )

            for host in hosts:
                if not host.ip_address:
                    continue
                try:
                    host_cert, host_key = sign_host_cert(
                        pool.ca_cert,
                        pool.ca_key,
                        host.ip_address,
                        host.private_ip or "",
                    )
                    job_id = start_job(
                        host,
                        "/tls/update-certs",
                        {
                            "ca_cert_b64": base64.b64encode(
                                pool.ca_cert.encode()
                            ).decode(),
                            "host_cert_b64": base64.b64encode(
                                host_cert.encode()
                            ).decode(),
                            "host_key_b64": base64.b64encode(
                                host_key.encode()
                            ).decode(),
                        },
                    )
                    wait_for_job(host, job_id, timeout=30)
                    logger.debug("Renewed TLS cert for host %s", host.id[:8])
                except Exception:
                    logger.debug(
                        "Cert renewal failed for host %s", host.id[:8], exc_info=True
                    )
    except Exception:
        logger.debug("Cert renewal check failed", exc_info=True)
    finally:
        db.close()


def _poller_loop():
    """Background loop — polls forever."""
    logger.info(
        "Health poller started (interval=%ds, disconnect_after=%ds)",
        _INTERVAL_SECONDS,
        _DISCONNECT_AFTER_SECONDS,
    )
    while True:
        time.sleep(_INTERVAL_SECONDS)
        try:
            _poll_hosts()
            _check_cert_renewal()
        except Exception:
            logger.exception("Health poller error")
        try:
            from app.core.rate_limit import cleanup as _rl_cleanup

            _rl_cleanup()
        except Exception:
            pass


def start_health_poller():
    """Start the background health poller thread. Call once at app startup."""
    thread = threading.Thread(target=_poller_loop, daemon=True, name="health-poller")
    thread.start()
    return thread
