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
from datetime import datetime, timezone

from app.core.config import config

logger = logging.getLogger(__name__)

# Read intervals from config with sensible defaults
_health_config = getattr(config, 'health', None)
_INTERVAL_SECONDS = getattr(_health_config, 'interval_seconds', 30) if _health_config else 30
_DISCONNECT_AFTER_SECONDS = getattr(_health_config, 'disconnect_after_seconds', 90) if _health_config else 90


def _poll_hosts():
    """Single poll cycle — check all active hosts."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.services.troshkad_client import check_health

    db = SessionLocal()
    try:
        # Query hosts that should be polled:
        # - state="active" (not stopped/terminated)
        # - have agent_token (troshkad is installed)
        hosts = db.query(Host).filter(
            Host.state == "active",
            Host.agent_token.isnot(None),
        ).all()

        for host in hosts:
            try:
                health = check_health(host)
                now = datetime.now(timezone.utc)

                if health:
                    # Success — update health data
                    host.last_health_at = now
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

                    # Auto-reconnect if was disconnected
                    if host.agent_status == "disconnected":
                        host.agent_status = "connected"
                        logger.info("Host %s reconnected (troshkad %s)", host.id[:8], health.get("version"))
                else:
                    # Failed — check if we should mark as disconnected
                    if host.agent_status == "connected" and host.last_health_at:
                        elapsed = (now - host.last_health_at).total_seconds()
                        if elapsed > _DISCONNECT_AFTER_SECONDS:
                            host.agent_status = "disconnected"
                            logger.warning("Host %s marked disconnected (no health for %ds)", host.id[:8], int(elapsed))
            except Exception:
                logger.debug("Health check failed for host %s", host.id[:8], exc_info=True)

        db.commit()
    except Exception:
        logger.exception("Health poller cycle failed")
    finally:
        db.close()


def _poller_loop():
    """Background loop — polls forever."""
    logger.info("Health poller started (interval=%ds, disconnect_after=%ds)", _INTERVAL_SECONDS, _DISCONNECT_AFTER_SECONDS)
    while True:
        time.sleep(_INTERVAL_SECONDS)
        try:
            _poll_hosts()
        except Exception:
            logger.exception("Health poller error")


def start_health_poller():
    """Start the background health poller thread. Call once at app startup."""
    thread = threading.Thread(target=_poller_loop, daemon=True, name="health-poller")
    thread.start()
    return thread
