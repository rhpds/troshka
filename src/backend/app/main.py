import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("uvicorn.access").handlers = []
logging.getLogger("uvicorn.access").propagate = True

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.auth import require_role
from app.core.config import config
from app.core.database import init_db

logger = logging.getLogger(__name__)

init_db()


@asynccontextmanager
async def lifespan(app):
    import asyncio

    from app.services.health_poller import start_health_poller
    from app.services.project_timer import start_project_timer
    from app.services.ws_pubsub import set_event_loop, start_state_poller

    set_event_loop(asyncio.get_running_loop())
    start_health_poller()
    start_project_timer()
    start_state_poller()

    # Reset projects stuck in transient states from a previous crash/restart
    from app.core.database import SessionLocal
    from app.models.project import Project

    import threading

    s = SessionLocal()
    try:
        stuck = (
            s.query(Project)
            .filter(
                Project.state.in_(
                    ("deploying", "reconfiguring", "starting", "stopping")
                )
            )
            .all()
        )
        for p in stuck:
            old_state = p.state
            if old_state == "deploying" and p.deploy_step:
                logger.info(
                    "Startup: resuming deploy for %s (%s) from step '%s'",
                    p.name,
                    p.id[:8],
                    p.deploy_step,
                )
                from app.services.deploy_service import deploy_project_async

                threading.Thread(
                    target=deploy_project_async,
                    args=(p.id,),
                    kwargs={"resume_from": p.deploy_step},
                    name=f"deploy-{p.id[:8]}",
                    daemon=True,
                ).start()
            else:
                logger.warning(
                    "Startup: resetting stuck project %s (%s) from %s to error",
                    p.name,
                    p.id[:8],
                    old_state,
                )
                p.state = "error"
                p.deploy_error = f"Server restarted while project was {old_state}"
        if stuck:
            s.commit()
    finally:
        s.close()

    # Reset hosts stuck in transient agent states from a previous crash/restart
    from app.models.host import Host as _HostReset

    s2 = SessionLocal()
    try:
        stuck_hosts = (
            s2.query(_HostReset)
            .filter(
                _HostReset.agent_status.in_(
                    ("waiting_ssh", "installing", "install_failed")
                )
            )
            .all()
        )
        for h in stuck_hosts:
            logger.warning(
                "Startup: resetting stuck host %s agent_status from %s to disconnected",
                h.id[:8],
                h.agent_status,
            )
            h.agent_status = "disconnected"
        if stuck_hosts:
            s2.commit()
    finally:
        s2.close()

    # Resume stuck pattern captures
    from app.models.pattern import Pattern

    s = SessionLocal()
    try:
        stuck_patterns = s.query(Pattern).filter(Pattern.state == "capturing").all()
        for pat in stuck_patterns:
            logger.info(
                "Startup: resuming pattern capture %s (%s)",
                pat.name,
                pat.id[:8],
            )
            from app.services.pattern_service import capture_pattern_disks

            threading.Thread(
                target=capture_pattern_disks,
                args=(pat.id, pat.source_project_id, False),
                name=f"capture-{pat.id[:8]}",
                daemon=True,
            ).start()
    finally:
        s.close()

    # Resume polling for storage pools stuck in "creating" (poller thread died on restart)
    from app.models.provider import Provider
    from app.models.storage_pool import StoragePool

    s = SessionLocal()
    try:
        creating_pools = (
            s.query(StoragePool).filter(StoragePool.status == "creating").all()
        )
        for pool in creating_pools:
            if pool.fsx_filesystem_id:
                provider = s.query(Provider).get(pool.provider_id)
                if provider:
                    creds = provider.get_credentials()
                    from app.services.storage_pool_service import (
                        _poll_fsx_until_available,
                    )

                    logger.info(
                        "Startup: resuming FSx poller for pool %s (%s)",
                        pool.name,
                        pool.fsx_filesystem_id,
                    )
                    threading.Thread(
                        target=_poll_fsx_until_available,
                        args=(
                            pool.id,
                            creds,
                            provider.default_region,
                            pool.fsx_filesystem_id,
                        ),
                        daemon=True,
                    ).start()
            else:
                logger.warning(
                    "Startup: pool %s stuck in creating with no FSx ID, marking error",
                    pool.name,
                )
                pool.status = "error"

        # Ensure SG rules are up-to-date for all available shared pools
        from app.services.storage_pool_service import add_sg_rules_for_shared_storage

        available_pools = (
            s.query(StoragePool)
            .filter(StoragePool.status == "available", StoragePool.mode == "shared-fsx")
            .all()
        )
        for pool in available_pools:
            provider = s.query(Provider).get(pool.provider_id)
            if provider and provider.security_group_id:
                try:
                    creds = provider.get_credentials()
                    add_sg_rules_for_shared_storage(
                        creds, provider.default_region, provider.security_group_id
                    )
                    logger.info("Startup: synced SG rules for pool %s", pool.name)
                except Exception as e:
                    logger.warning(
                        "Startup: failed to sync SG rules for pool %s: %s", pool.name, e
                    )

        # Resume stuck pattern buffer installs (agent disconnected but host active)
        from app.models.host import Host as _Host
        from app.models.storage_pool import StoragePool

        pb_pools = (
            s.query(StoragePool).filter(StoragePool.worker_host_id.isnot(None)).all()
        )
        for pool in pb_pools:
            pb_host = s.query(_Host).filter_by(id=pool.worker_host_id).first()
            if (
                pb_host
                and pb_host.state == "active"
                and pb_host.agent_status != "connected"
            ):
                logger.info(
                    "Startup: retrying agent install on pattern buffer %s for pool %s",
                    pb_host.id[:8],
                    pool.name,
                )
                threading.Thread(
                    target=_retry_pb_agent_install,
                    args=(pb_host.id, pool.id),
                    name=f"pb-retry-{pb_host.id[:8]}",
                    daemon=True,
                ).start()

        s.commit()
    finally:
        s.close()

    yield


def _retry_pb_agent_install(host_id: str, pool_id: str):
    """Retry agent install on a pattern buffer host that got stuck."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.storage_pool import StoragePool
    from app.services.agent_deployer import (
        deploy_agent,
        get_provider_data_disk,
        get_provider_ssh_port,
        get_provider_ssh_user,
        wait_for_ssh,
    )

    db = SessionLocal()
    try:
        host = db.query(Host).filter_by(id=host_id).first()
        pool = db.query(StoragePool).filter_by(id=pool_id).first()
        if not host or not pool or not pool.provider:
            return

        provider = pool.provider
        ssh_user = get_provider_ssh_user(provider.type)
        ssh_host = host.ip_address
        ssh_port = get_provider_ssh_port(provider.type)

        if not wait_for_ssh(
            ssh_host, host.private_key, port=ssh_port, ssh_user=ssh_user, timeout=120
        ):
            logger.warning("PB retry: SSH not available on %s", host_id[:8])
            return

        data_disk = get_provider_data_disk(provider.type)
        storage_mode = (
            "shared"
            if pool.nfs_endpoint or pool.fsx_dns_name or pool.azure_file_share_url
            else "local"
        )
        cert_pem = key_pem = ca_pem = ""
        if pool.ca_cert and pool.ca_key:
            from app.services.storage_pool_service import sign_host_cert

            cert_pem, key_pem = sign_host_cert(
                pool.ca_cert,
                pool.ca_key,
                host.ip_address,
                host.private_ip or "",
            )
            ca_pem = pool.ca_cert

        nfs_server = nfs_path = ""
        if pool.fsx_dns_name:
            nfs_server, nfs_path = pool.fsx_dns_name, "/fsx"
        elif pool.azure_file_share_url:
            parts = pool.azure_file_share_url.split(":", 1)
            nfs_server = parts[0]
            nfs_path = parts[1] if len(parts) > 1 else "/"
        elif pool.nfs_endpoint:
            parts = pool.nfs_endpoint.split(":", 1)
            nfs_server = parts[0]
            nfs_path = parts[1] if len(parts) > 1 else "/"

        deploy_agent(
            ssh_host,
            host.private_key or "",
            host_id=host_id,
            storage_mode=storage_mode,
            host_cert=cert_pem,
            host_key=key_pem,
            ca_cert=ca_pem,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            data_disk_device=data_disk,
            nfs_server=nfs_server,
            nfs_path=nfs_path,
            nfs_port=pool.nfs_port or 0,
        )
        logger.info("PB retry: agent installed on %s", host_id[:8])
    except Exception:
        logger.exception("PB retry: failed for %s", host_id[:8])
    finally:
        db.close()


app = FastAPI(
    title=config.app.name,
    description="Nested VM Environment Builder",
    version="0.1.0",
    root_path=config.app.root_path,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api import api_keys as api_key_routes  # noqa: E402
from app.api import auth as auth_routes  # noqa: E402
from app.api import disks as disk_routes  # noqa: E402
from app.api import dns_providers as dns_provider_routes  # noqa: E402
from app.api import eips as eip_routes  # noqa: E402
from app.api import hosts as host_routes  # noqa: E402
from app.api import library as library_routes  # noqa: E402
from app.api import networks as network_routes  # noqa: E402
from app.api import patterns as pattern_routes  # noqa: E402
from app.api import portal as portal_routes  # noqa: E402
from app.api import projects as project_routes  # noqa: E402
from app.api import providers as provider_routes  # noqa: E402
from app.api import registry_credential_routes as registry_cred_routes  # noqa: E402
from app.api import storage_pools as storage_pool_routes  # noqa: E402
from app.api import templates as template_routes  # noqa: E402
from app.api import vms as vm_routes  # noqa: E402
from app.api import ws as ws_routes  # noqa: E402

app.include_router(auth_routes.router, prefix="/api/v1")
app.include_router(project_routes.router, prefix="/api/v1")
app.include_router(vm_routes.router, prefix="/api/v1")
app.include_router(network_routes.router, prefix="/api/v1")
app.include_router(disk_routes.router, prefix="/api/v1")
app.include_router(api_key_routes.router, prefix="/api/v1")
app.include_router(host_routes.router, prefix="/api/v1")
app.include_router(provider_routes.router, prefix="/api/v1")
app.include_router(library_routes.router, prefix="/api/v1")
app.include_router(pattern_routes.router, prefix="/api/v1")
app.include_router(eip_routes.router, prefix="/api/v1")
app.include_router(ws_routes.router)
app.include_router(storage_pool_routes.router, prefix="/api/v1")
app.include_router(dns_provider_routes.router, prefix="/api/v1")
app.include_router(portal_routes.router, prefix="/api/v1")
app.include_router(template_routes.router, prefix="/api/v1")
app.include_router(registry_cred_routes.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health_check():
    return {"status": "healthy", "app": config.app.name, "version": "0.1.0"}


@app.get("/api/v1/ocp/versions")
def ocp_versions():
    """Fetch available OCP stable versions from the OpenShift Update Service."""
    import urllib.request

    channels = []
    for minor in range(18, 25):
        channel = f"stable-4.{minor}"
        try:
            req = urllib.request.Request(
                f"https://api.openshift.com/api/upgrades_info/v1/graph?channel={channel}&arch=amd64",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json

                data = json.loads(resp.read())
                versions = sorted(set(n["version"] for n in data.get("nodes", [])))
                if versions:
                    channels.append(
                        {
                            "channel": channel,
                            "minor": f"4.{minor}",
                            "latest": versions[-1],
                            "count": len(versions),
                        }
                    )
        except Exception:
            continue
    return channels


@app.get("/api/v1/debug/threads")
def debug_threads(user=Depends(require_role("admin"))):
    import threading

    threads = []
    for t in threading.enumerate():
        threads.append({"name": t.name, "daemon": t.daemon, "alive": t.is_alive()})
    return {"count": len(threads), "threads": threads}
