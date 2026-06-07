import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import config
from app.core.database import init_db

init_db()

app = FastAPI(
    title=config.app.name,
    description="Nested VM Environment Builder",
    version="0.1.0",
    root_path=config.app.root_path,
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

app.include_router(auth_routes.router, prefix="/api/v1")
app.include_router(project_routes.router, prefix="/api/v1")
app.include_router(vm_routes.router, prefix="/api/v1")
app.include_router(network_routes.router, prefix="/api/v1")
app.include_router(disk_routes.router, prefix="/api/v1")
app.include_router(api_key_routes.router, prefix="/api/v1")
app.include_router(host_routes.router, prefix="/api/v1")
app.include_router(provider_routes.router, prefix="/api/v1")
app.include_router(library_routes.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health_check():
    return {"status": "healthy", "app": config.app.name, "version": "0.1.0"}
