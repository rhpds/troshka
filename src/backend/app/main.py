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
    from app.services.ws_pubsub import set_event_loop, start_state_poller

    set_event_loop(asyncio.get_running_loop())
    start_health_poller()
    start_state_poller()

    # Reset projects stuck in transient states from a previous crash/restart
    from app.core.database import SessionLocal
    from app.models.project import Project

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

    # Resume polling for storage pools stuck in "creating" (poller thread died on restart)
    import threading

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

        s.commit()
    finally:
        s.close()

    yield


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
