"""
RQ worker entrypoint for background deploy/destroy/start/stop jobs.

Run with:
    rq worker deploy provision default --url redis://localhost:6379/0

Workers execute the same functions that previously ran in daemon threads —
they already create their own DB sessions and run independently.
"""

import logging
import os
import sys

# Ensure the backend package is importable
_backend_dir = os.path.join(os.path.dirname(__file__), "..", "..")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s [worker]: %(message)s",
    datefmt="%H:%M:%S",
)

from app.core.database import init_db  # noqa: E402

init_db()


def run_worker():
    """Start an RQ worker that listens on deploy, provision, and default queues."""
    from redis import Redis
    from rq import Worker

    from app.core.config import config

    url = getattr(config, "redis", {})
    if isinstance(url, dict):
        url = url.get("url", "redis://localhost:6379/0")
    else:
        url = getattr(url, "url", "redis://localhost:6379/0")

    conn = Redis.from_url(url)
    queues = ["deploy", "provision", "default"]

    logger = logging.getLogger(__name__)
    logger.info("Starting RQ worker on queues: %s", ", ".join(queues))

    w = Worker(queues, connection=conn)
    w.work()


if __name__ == "__main__":
    run_worker()
