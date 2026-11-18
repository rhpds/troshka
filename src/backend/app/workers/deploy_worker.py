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
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:

    class _WorkerInvocation(Protocol):
        def perform_job(self, job: Any, queue: Any) -> bool:
            ...


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


def worker_queues(profile):
    if profile == "lifecycle":
        return ["project_lifecycle", "host_lifecycle", "default"]
    if profile == "collection":
        return ["native_cleanup", "native_collection"]
    if profile == "readiness":
        return ["native_readiness"]
    raise ValueError("Unknown RQ worker queue profile")


class NativeOriginWorkerMixin:
    """Retain completion of the exact RQ invocation, independently of job status."""

    def perform_job(self, job, queue):
        from app.services.native_origin_writers import run_execution

        invoke = cast("_WorkerInvocation", super()).perform_job
        return run_execution(self, job, lambda: invoke(job, queue))


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

    from app.core.redis import get_redis

    get_redis()  # ensure progress keys use Redis, not per-process memory

    conn = Redis.from_url(url)
    profile = os.environ.get("TROSHKA_WORKER_PROFILE", "lifecycle")
    queues = worker_queues(profile)
    worker_name = f"{_POD_NAME}.{os.getpid()}"

    if platform.system() == "Darwin":
        _logger.info("macOS detected — using SimpleWorker (no fork)")

        class _TroshkaSimpleWorker(NativeOriginWorkerMixin, SimpleWorker):
            def perform_job(self, job, queue):
                _log_job_context(">> START", job, self.name)
                _save_worker_meta(job)
                result = super().perform_job(job, queue)
                _log_job_context("<< DONE " if result else "<< FAIL ", job, self.name)
                return result

        worker_class = _TroshkaSimpleWorker
    else:

        class _TroshkaWorker(NativeOriginWorkerMixin, Worker):
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
    # Re-attach any ops-pod install monitor stranded by a prior worker exit
    # (the monitor is a daemon thread; a restart mid-install leaves ocp_status
    # at "monitoring" forever). A per-project Redis lock makes this safe across
    # all worker processes — only the first to acquire actually monitors.
    try:
        from app.services.deploy_service import resume_ops_pod_monitors

        if profile == "lifecycle":
            resume_ops_pod_monitors()
    except Exception:
        _logger.exception("resume_ops_pod_monitors on startup failed")

    # Re-attach workload run monitors stranded by a prior worker exit
    try:
        from app.services.workloads.run_service import resume_workload_monitors

        if profile == "lifecycle":
            resume_workload_monitors()
    except Exception:
        _logger.exception("resume_workload_monitors on startup failed")

    w = worker_class(queues, connection=conn, name=worker_name)
    w.work()


if __name__ == "__main__":
    run_worker()
