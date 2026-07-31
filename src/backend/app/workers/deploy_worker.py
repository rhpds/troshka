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

_logger = logging.getLogger(__name__)
_POD_NAME = os.environ.get("HOSTNAME", "local")


def _log_job_context(prefix: str, job, worker_name: str):
    """Log a structured routing line for job start/end."""
    project_id = job.meta.get("project_id", "")
    host_id = job.meta.get("host_id", "")
    func_name = (job.func_name or "").rsplit(".", 1)[-1]
    parts = [f"job={job.id[:8]}", f"func={func_name}"]
    if project_id:
        parts.append(f"project={project_id[:8]}")
    if host_id:
        parts.append(f"host={host_id[:8]}")
    parts.append(f"worker={worker_name}")
    _logger.info("%s %s", prefix, " ".join(parts))


def _save_worker_meta(job):
    """Store the worker pod name on the job for post-mortem lookup."""
    job.meta["worker_pod"] = _POD_NAME
    try:
        job.save_meta()
    except Exception:
        pass


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
    worker_name = f"{_POD_NAME}.{os.getpid()}"

    if platform.system() == "Darwin":
        _logger.info("macOS detected — using SimpleWorker (no fork)")

        class _TroshkaSimpleWorker(SimpleWorker):
            def perform_job(self, job, queue):
                _log_job_context(">> START", job, self.name)
                _save_worker_meta(job)
                result = super().perform_job(job, queue)
                _log_job_context("<< DONE " if result else "<< FAIL ", job, self.name)
                return result

        worker_class = _TroshkaSimpleWorker
    else:

        class _TroshkaWorker(Worker):
            def perform_job(self, job, queue):
                _log_job_context(">> START", job, self.name)
                _save_worker_meta(job)
                result = super().perform_job(job, queue)
                _log_job_context("<< DONE " if result else "<< FAIL ", job, self.name)
                return result

        worker_class = _TroshkaWorker

    _logger.info(
        "Starting worker on pod %s, queues: %s",
        _POD_NAME,
        ", ".join(queues),
    )
    w = worker_class(queues, connection=conn, name=worker_name)
    w.work()


if __name__ == "__main__":
    run_worker()
