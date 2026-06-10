import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import config
from app.core.database import init_db

logger = logging.getLogger(__name__)

init_db()


@asynccontextmanager
async def lifespan(app):
    from app.services.health_poller import start_health_poller
    from app.services.ws_pubsub import set_event_loop, start_state_poller
    import asyncio
    set_event_loop(asyncio.get_running_loop())
    start_health_poller()
    start_state_poller()

    # Reset projects stuck in transient states from a previous crash/restart
    from app.core.database import SessionLocal
    from app.models.project import Project
    s = SessionLocal()
    try:
        stuck = s.query(Project).filter(Project.state.in_(("deploying", "reconfiguring", "starting", "stopping"))).all()
        for p in stuck:
            old_state = p.state
            logger.warning("Startup: resetting stuck project %s (%s) from %s to error", p.name, p.id[:8], old_state)
            p.state = "error"
            p.deploy_error = f"Server restarted while project was {old_state}"
        if stuck:
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

from app.api import auth as auth_routes  # noqa: E402
from app.api import projects as project_routes  # noqa: E402
from app.api import vms as vm_routes  # noqa: E402
from app.api import networks as network_routes  # noqa: E402
from app.api import disks as disk_routes  # noqa: E402
from app.api import api_keys as api_key_routes  # noqa: E402
from app.api import hosts as host_routes  # noqa: E402
from app.api import providers as provider_routes  # noqa: E402
from app.api import library as library_routes  # noqa: E402
from app.api import patterns as pattern_routes  # noqa: E402
from app.api import eips as eip_routes  # noqa: E402
from app.api import ws as ws_routes  # noqa: E402
from app.api import storage_pools as storage_pool_routes  # noqa: E402

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


@app.get("/api/v1/health")
def health_check():
    return {"status": "healthy", "app": config.app.name, "version": "0.1.0"}
