"""
RQ worker entrypoint for background deploy/destroy/start/stop jobs.

Uses SimpleWorker on macOS (fork() crashes the ObjC runtime) and regular
Worker on Linux (fork isolates each job in a child process).

Each job function imports what it needs and creates its own DB session.
"""

import logging
import os
import platform
import sys

_backend_dir = os.path.join(os.path.dirname(__file__), "..", "..")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s [worker]: %(message)s",
    datefmt="%H:%M:%S",
)


def run_worker():
    """Start an RQ worker that listens on deploy, provision, and default queues."""
    from redis import Redis
    from rq import SimpleWorker, Worker

    from app.core.config import config

    url = getattr(config, "redis", {})
    if isinstance(url, dict):
        url = url.get("url", "redis://localhost:6379/0")
    else:
        url = getattr(url, "url", "redis://localhost:6379/0")

    conn = Redis.from_url(url)
    queues = ["project_lifecycle", "host_lifecycle", "default"]

    logger = logging.getLogger(__name__)

    # macOS: fork() crashes the ObjC runtime — use SimpleWorker (no fork)
    # Linux: use regular Worker (fork gives job isolation)
    if platform.system() == "Darwin":
        logger.info("macOS detected — using SimpleWorker (no fork)")
        worker_class = SimpleWorker
    else:
        worker_class = Worker

    logger.info("Starting RQ worker on queues: %s", ", ".join(queues))
    w = worker_class(queues, connection=conn)
    w.work()


if __name__ == "__main__":
    run_worker()
