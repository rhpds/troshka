"""ASGI entrypoint for the dedicated Troshka tunnel service.

Run (dev)::

    cd src/backend && ./venv/bin/uvicorn app.tunnel_main:app --host 0.0.0.0 --port 8201

Prod Helm uses the backend image with this module as the container command.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

init_db()

from app.tunnel.ws import router as tunnel_router  # noqa: E402

app = FastAPI(title="Troshka Tunnel", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(tunnel_router)


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "service": "tunnel"}
