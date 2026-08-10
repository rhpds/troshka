import faulthandler
import logging
import signal
import sys

faulthandler.enable()
faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("uvicorn.access").handlers = []
logging.getLogger("uvicorn.access").propagate = True

from contextlib import asynccontextmanager
from datetime import UTC
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.auth import require_role
from app.core.config import config
from app.core.database import init_db

logger = logging.getLogger(__name__)

init_db()

from app.models.user import User  # noqa: E402

AdminUser = Annotated[User, Depends(require_role("admin"))]


def _startup_clear_health_monitors():
    """Clear stale OCP health monitor set entries from previous pod."""
    from app.core.redis import is_redis_available

    if not is_redis_available():
        return
    try:
        from app.core.redis import get_redis

        r = get_redis()
        r.delete("deploy:health_monitors")
        logger.info("Startup: cleared health monitor set")
    except Exception:
        pass


def _is_stale_abandoned_job(j, now_utc) -> bool:
    """Return True if the abandoned job ended more than 1 hour ago."""

    job_ended = j.ended_at
    if not job_ended:
        return False
    if job_ended.tzinfo is None:
        job_ended = job_ended.replace(tzinfo=UTC)
    return (now_utc - job_ended).total_seconds() > 3600


def _should_discard_for_project(j, db) -> bool:
    """Return True if the job's project is gone or no longer in a transient state."""
    from app.models.project import Project

    pid = j.meta.get("project_id")
    if not pid:
        return False
    proj = db.get(Project, pid)
    if not proj:
        return True
    return proj.state not in ("deploying", "starting", "stopping", "reconfiguring")


def _handle_abandoned_job(j, registry, db, now_utc):
    """Process a single abandoned job: discard if stale/irrelevant, otherwise re-queue."""
    if "AbandonedJobError" not in str(j.exc_info or ""):
        return

    func_label = (j.func_name or "unknown").split(".")[-1]
    jid = j.id

    if _is_stale_abandoned_job(j, now_utc):
        logger.info(
            "Startup: discarding stale abandoned job %s (%s)",
            jid[:8],
            func_label,
        )
        registry.remove(j)
        j.delete()
        return

    if _should_discard_for_project(j, db):
        pid = j.meta.get("project_id", "")
        logger.info(
            "Startup: discarding abandoned job %s — project %s not applicable",
            jid[:8],
            pid[:8] if pid else "none",
        )
        registry.remove(j)
        j.delete()
        return

    logger.warning(
        "Startup: re-queuing abandoned job %s (%s)",
        jid[:8],
        func_label,
    )
    registry.remove(j)
    j.requeue()


def _startup_recover_abandoned_jobs():
    """Clean up abandoned RQ jobs (workers killed during rollout)."""
    from app.core.redis import is_redis_available

    if not is_redis_available():
        return
    try:
        from datetime import datetime

        from rq import Queue
        from rq.job import Job

        from app.core.database import SessionLocal
        from app.core.redis import get_redis_raw

        r = get_redis_raw()
        db = SessionLocal()
        try:
            now_utc = datetime.now(UTC)
            for qname in ["project_lifecycle", "host_lifecycle", "default"]:
                q = Queue(qname, connection=r)
                registry = q.failed_job_registry
                abandoned_ids = registry.get_job_ids()
                for jid in abandoned_ids:
                    try:
                        j = Job.fetch(jid, connection=r)
                        _handle_abandoned_job(j, registry, db, now_utc)
                    except Exception:
                        pass
        finally:
            db.close()
    except Exception:
        logger.warning("Startup: failed to check abandoned jobs")


def _startup_reset_stuck_projects():
    """Reset projects stuck in transient states from a previous crash/restart."""
    from app.core.database import SessionLocal
    from app.core.redis import enqueue_job, is_redis_available
    from app.models.project import Project

    db = SessionLocal()
    try:
        stuck = (
            db.query(Project)
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

                enqueue_job(
                    deploy_project_async,
                    p.id,
                    resume_from=p.deploy_step,
                )
            elif is_redis_available():
                from app.core.redis import get_job_info

                job_info = get_job_info(p.id)
                if job_info and job_info.get("status") in ("queued", "started"):
                    logger.info(
                        "Startup: project %s (%s) has active RQ job, skipping",
                        p.name,
                        p.id[:8],
                    )
                    continue
                logger.warning(
                    "Startup: resetting stuck project %s (%s) from %s to error",
                    p.name,
                    p.id[:8],
                    old_state,
                )
                p.state = "error"
                p.deploy_error = f"Server restarted while project was {old_state}"
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
            db.commit()
    finally:
        db.close()


def _startup_reset_stuck_hosts():
    """Reset hosts stuck in transient agent states from a previous crash/restart."""
    from app.core.database import SessionLocal
    from app.models.host import Host

    db = SessionLocal()
    try:
        stuck_hosts = (
            db.query(Host)
            .filter(
                Host.agent_status.in_(("waiting_ssh", "installing", "install_failed"))
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
            db.commit()
    finally:
        db.close()


def _startup_resume_pattern_captures():
    """Resume stuck pattern captures."""
    from app.core.database import SessionLocal
    from app.core.redis import enqueue_job
    from app.models.pattern import Pattern

    db = SessionLocal()
    try:
        stuck_patterns = db.query(Pattern).filter(Pattern.state == "capturing").all()
        for pat in stuck_patterns:
            logger.info(
                "Startup: resuming pattern capture %s (%s)",
                pat.name,
                pat.id[:8],
            )
            from app.services.pattern_service import capture_pattern_disks

            enqueue_job(capture_pattern_disks, pat.id, pat.source_project_id, False)
    finally:
        db.close()


def _resume_creating_pools(db, enqueue_job, provider_model):
    """Resume FSx pollers for pools stuck in 'creating' state."""
    from app.models.storage_pool import StoragePool

    creating_pools = (
        db.query(StoragePool).filter(StoragePool.status == "creating").all()
    )
    for pool in creating_pools:
        if not pool.fsx_filesystem_id:
            logger.warning(
                "Startup: pool %s stuck in creating with no FSx ID, marking error",
                pool.name,
            )
            pool.status = "error"
            continue
        provider = db.get(provider_model, pool.provider_id)
        if not provider:
            continue
        creds = provider.get_credentials()
        from app.services.storage_pool_service import (
            _poll_fsx_until_available,
        )

        logger.info(
            "Startup: resuming FSx poller for pool %s (%s)",
            pool.name,
            pool.fsx_filesystem_id,
        )
        enqueue_job(
            _poll_fsx_until_available,
            pool.id,
            creds,
            provider.default_region,
            pool.fsx_filesystem_id,
            queue_name="host_lifecycle",
        )


def _sync_shared_pool_sg_rules(db, provider_model):
    """Ensure SG rules are up-to-date for all available shared-fsx pools."""
    from app.models.storage_pool import StoragePool
    from app.services.storage_pool_service import add_sg_rules_for_shared_storage

    available_pools = (
        db.query(StoragePool)
        .filter(StoragePool.status == "available", StoragePool.mode == "shared-fsx")
        .all()
    )
    for pool in available_pools:
        provider = db.get(provider_model, pool.provider_id)
        if not provider or not provider.security_group_id:
            continue
        try:
            creds = provider.get_credentials()
            add_sg_rules_for_shared_storage(
                creds,
                provider.default_region or "",
                provider.security_group_id,
            )
            logger.info("Startup: synced SG rules for pool %s", pool.name)
        except Exception as e:
            logger.warning(
                "Startup: failed to sync SG rules for pool %s: %s", pool.name, e
            )


def _retry_stuck_pattern_buffer_installs(db, enqueue_job, host_model):
    """Retry agent install on pattern buffer hosts that are active but disconnected."""
    from app.models.storage_pool import StoragePool

    pb_pools = (
        db.query(StoragePool).filter(StoragePool.worker_host_id.isnot(None)).all()
    )
    for pool in pb_pools:
        pb_host = db.query(host_model).filter_by(id=pool.worker_host_id).first()
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
            enqueue_job(
                _retry_pb_agent_install,
                pb_host.id,
                pool.id,
                queue_name="host_lifecycle",
            )


def _startup_resume_storage_pools():
    """Resume storage pool operations, sync SG rules, and retry pattern buffer installs."""
    from app.core.database import SessionLocal
    from app.core.redis import enqueue_job
    from app.models.host import Host
    from app.models.provider import Provider

    db = SessionLocal()
    try:
        _resume_creating_pools(db, enqueue_job, Provider)
        _sync_shared_pool_sg_rules(db, Provider)
        _retry_stuck_pattern_buffer_installs(db, enqueue_job, Host)
        db.commit()
    finally:
        db.close()


def _startup_sync_obc_credentials():
    """Sync OBC S3 credentials from each KubeVirt cluster into provider records."""
    from app.core.database import SessionLocal
    from app.models.provider import Provider

    db = SessionLocal()
    try:
        providers = db.query(Provider).filter_by(type="kubevirt", state="active").all()
        for provider in providers:
            try:
                _sync_provider_obc(db, provider)
            except Exception:
                logger.debug(
                    "OBC sync skipped for %s (cluster may be unreachable)",
                    provider.name,
                )
        db.commit()
    except Exception:
        logger.debug("OBC credential sync failed", exc_info=True)
    finally:
        db.close()


def _sync_provider_obc(db, provider):
    """Read OBC credentials from a KubeVirt cluster and store in provider record."""
    import base64

    from app.services.providers.kubevirt import _get_k8s_clients

    _, core_api, _ = _get_k8s_clients(provider)

    obc_name = "troshka-patterns"
    ns = "troshka-operator"
    secret = core_api.read_namespaced_secret(obc_name, ns)
    cm = core_api.read_namespaced_config_map(obc_name, ns)

    secret_data = getattr(secret, "data", None) or {}
    cm_data = getattr(cm, "data", None) or {}
    s3_config = {
        "bucket": cm_data.get("BUCKET_NAME", ""),
        "endpoint": (
            "http://rook-ceph-rgw-ocs-storagecluster-cephobjectstore"
            ".openshift-storage.svc:80"
        ),
        "region": cm_data.get("BUCKET_REGION", "us-east-1") or "us-east-1",
        "access_key_id": base64.b64decode(
            secret_data.get("AWS_ACCESS_KEY_ID", "")
        ).decode(),
        "secret_access_key": base64.b64decode(
            secret_data.get("AWS_SECRET_ACCESS_KEY", "")
        ).decode(),
    }

    creds = provider.get_credentials()
    if creds.get("s3_config") != s3_config:
        creds["s3_config"] = s3_config
        provider.set_credentials(creds)
        logger.info("Synced OBC credentials for provider %s", provider.name)


@asynccontextmanager
async def lifespan(app):
    import asyncio

    from app.core.redis import get_redis
    from app.services.health_poller import start_health_poller
    from app.services.project_timer import start_project_timer
    from app.services.ws_pubsub import (
        set_event_loop,
        start_redis_listener,
        start_state_poller,
    )

    set_event_loop(asyncio.get_running_loop())

    # Initialize Redis connection
    try:
        r = get_redis()
        r.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning("Redis not available — falling back to local-only mode: %s", e)

    from app.services.agent_ca_service import ensure_agent_ca

    ensure_agent_ca()

    from app.services.s3_storage import ensure_dev_bucket_exists

    ensure_dev_bucket_exists()

    start_health_poller()
    start_project_timer()
    start_state_poller()
    start_redis_listener()

    from app.services.operator_updater import start_operator_updater

    start_operator_updater()

    _startup_clear_health_monitors()
    _startup_recover_abandoned_jobs()

    _startup_reset_stuck_projects()
    _startup_reset_stuck_hosts()

    _startup_resume_pattern_captures()
    _startup_resume_storage_pools()
    _startup_sync_obc_credentials()

    yield

    # Shutdown
    from app.core.redis import close_redis

    close_redis()


def _resolve_pool_nfs_config(pool) -> tuple:
    """Extract NFS server and path from a storage pool's endpoint config."""
    if pool.fsx_dns_name:
        return pool.fsx_dns_name, "/fsx"
    endpoint = pool.azure_file_share_url or pool.nfs_endpoint
    if endpoint:
        parts = endpoint.split(":", 1)
        return parts[0], parts[1] if len(parts) > 1 else "/"
    return "", ""


def _resolve_pool_tls_certs(pool, host) -> tuple:
    """Sign a host TLS cert from the pool CA, returning (cert, key, ca) PEM strings."""
    if not pool.ca_cert or not pool.ca_key:
        return "", "", ""
    from app.services.storage_pool_service import sign_host_cert

    cert_pem, key_pem = sign_host_cert(
        pool.ca_cert,
        pool.ca_key,
        host.ip_address or "",
        host.private_ip or "",
    )
    return cert_pem, key_pem, pool.ca_cert


def _retry_pb_agent_install(host_id: str, pool_id: str):
    """Retry agent install on a pattern buffer host that got stuck."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.storage_pool import StoragePool
    from app.services.agent_deployer import (
        AgentDeployConfig,
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
            ssh_host or "",
            host.private_key or "",
            port=ssh_port,
            ssh_user=ssh_user,
            timeout=120,
        ):
            logger.warning("PB retry: SSH not available on %s", host_id[:8])
            return

        data_disk = get_provider_data_disk(provider.type)
        storage_mode = (
            "shared"
            if pool.nfs_endpoint or pool.fsx_dns_name or pool.azure_file_share_url
            else "local"
        )
        cert_pem, key_pem, ca_pem = _resolve_pool_tls_certs(pool, host)
        nfs_server, nfs_path = _resolve_pool_nfs_config(pool)

        from app.services.agent_ca_service import get_agent_ca_cert

        deploy_agent(
            ssh_host or "",
            host.private_key or "",
            host_id=host_id,
            config=AgentDeployConfig(
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
                agent_ca_cert=get_agent_ca_cert(),
            ),
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

from app.core.rate_limit import RateLimitMiddleware  # noqa: E402

app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError):
    """Convert unguarded service-layer ValueErrors (e.g. missing S3 config)
    into a friendly 400 response instead of an uncaught 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})

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
from app.api import users as user_routes  # noqa: E402
from app.api import vms as vm_routes  # noqa: E402
from app.api import ws as ws_routes  # noqa: E402

_API_PREFIX = "/api/v1"

app.include_router(auth_routes.router, prefix=_API_PREFIX)
app.include_router(project_routes.router, prefix=_API_PREFIX)
app.include_router(vm_routes.router, prefix=_API_PREFIX)
app.include_router(network_routes.router, prefix=_API_PREFIX)
app.include_router(disk_routes.router, prefix=_API_PREFIX)
app.include_router(api_key_routes.router, prefix=_API_PREFIX)
app.include_router(host_routes.router, prefix=_API_PREFIX)
app.include_router(provider_routes.router, prefix=_API_PREFIX)
app.include_router(library_routes.router, prefix=_API_PREFIX)
app.include_router(pattern_routes.router, prefix=_API_PREFIX)
app.include_router(eip_routes.router, prefix=_API_PREFIX)
app.include_router(ws_routes.router)
app.include_router(storage_pool_routes.router, prefix=_API_PREFIX)
app.include_router(dns_provider_routes.router, prefix=_API_PREFIX)
app.include_router(portal_routes.router, prefix=_API_PREFIX)
app.include_router(template_routes.router, prefix=_API_PREFIX)
app.include_router(registry_cred_routes.router, prefix=_API_PREFIX)
app.include_router(user_routes.router, prefix=_API_PREFIX)


@app.get(f"{_API_PREFIX}/health")
def health_check():
    return {"status": "healthy", "app": config.app.name, "version": "0.1.0"}


@app.get(f"{_API_PREFIX}/ocp/versions")
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
                versions = sorted({n["version"] for n in data.get("nodes", [])})
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


@app.get(f"{_API_PREFIX}/debug/threads")
def debug_threads(user: AdminUser):
    import threading

    threads = []
    for t in threading.enumerate():
        threads.append({"name": t.name, "daemon": t.daemon, "alive": t.is_alive()})
    return {"count": len(threads), "threads": threads}


def _collect_queue_info(r):
    """Collect depth/status info for each RQ queue."""
    from rq import Queue

    queues_info = []
    for qname in ["project_lifecycle", "host_lifecycle", "default"]:
        try:
            q = Queue(qname, connection=r)
            queues_info.append(
                {
                    "name": qname,
                    "queued": q.count,
                    "started": q.started_job_registry.count,
                    "failed": q.failed_job_registry.count,
                    "deferred": q.deferred_job_registry.count,
                }
            )
        except Exception:
            queues_info.append({"name": qname, "error": "could not read queue"})
    return queues_info


def _collect_worker_info(r):
    """Collect status info for all RQ workers."""
    from rq import Worker

    workers = []
    try:
        for w in Worker.all(connection=r):
            cj = w.get_current_job()
            info: dict = {
                "name": w.name,
                "state": w.get_state(),
                "queues": [q.name for q in w.queues],
                "current_job": str(w.get_current_job_id() or ""),
                "current_queue": cj.origin if cj else "",
                "current_func": ((cj.func_name or "").split(".")[-1] if cj else ""),
                "successful_count": w.successful_job_count,
                "failed_count": w.failed_job_count,
                "total_working_time": w.total_working_time,
            }
            if cj:
                meta = cj.meta or {}
                info["current_project"] = (meta.get("project_id") or "")[:8]
                info["current_host"] = (meta.get("host_id") or "")[:8]
            workers.append(info)
    except Exception:
        pass
    return workers


def _collect_inflight_deploys(r_str):
    """Collect per-host in-flight deploy counts from Redis."""
    inflight = {}
    try:
        for key in r_str.scan_iter("inflight:deploys:*"):
            host_id = str(key).replace("inflight:deploys:", "")
            count = int(r_str.get(key) or 0)
            if count > 0:
                inflight[host_id[:8]] = count
    except Exception:
        pass
    return inflight


@app.get(f"{_API_PREFIX}/admin/queue-status")
def queue_status(user: AdminUser):
    """Show job queue depths, active workers, and failed jobs."""
    from app.core.redis import is_redis_available

    if not is_redis_available():
        return {
            "redis": False,
            "message": "Redis not available — running in single-process mode",
        }

    from app.core.redis import get_redis, get_redis_raw

    r = get_redis_raw()
    workers = _collect_worker_info(r)

    return {
        "redis": True,
        "queues": _collect_queue_info(r),
        "workers": workers,
        "worker_count": len(workers),
        "inflight_deploys": _collect_inflight_deploys(get_redis()),
    }


@app.get(f"{_API_PREFIX}/admin/failed-jobs")
def list_failed_jobs(
    user: AdminUser,
    queue_name: str = "deploy",
):
    """List failed jobs with error details."""
    from app.core.redis import is_redis_available

    if not is_redis_available():
        return {"jobs": []}

    from app.core.redis import get_redis_raw

    r = get_redis_raw()
    try:
        from rq import Queue
        from rq.job import Job

        q = Queue(queue_name, connection=r)
        failed_ids = q.failed_job_registry.get_job_ids()
        jobs = []
        for jid in failed_ids[:50]:
            try:
                job = Job.fetch(jid, connection=r)
                meta = job.meta or {}
                jobs.append(
                    {
                        "id": jid,
                        "func": job.func_name,
                        "args": [str(a)[:100] for a in (job.args or [])],
                        "error": str(job.exc_info or "")[:500],
                        "enqueued_at": (
                            job.enqueued_at.isoformat() if job.enqueued_at else None
                        ),
                        "ended_at": job.ended_at.isoformat() if job.ended_at else None,
                        "project": (meta.get("project_id") or "")[:8],
                        "host": (meta.get("host_id") or "")[:8],
                        "worker_pod": meta.get("worker_pod", ""),
                    }
                )
            except Exception:
                jobs.append({"id": jid, "error": "could not fetch"})
        return {"queue": queue_name, "count": len(failed_ids), "jobs": jobs}
    except Exception as e:
        return {"error": str(e)}


@app.post(
    f"{_API_PREFIX}/admin/failed-jobs/{{job_id}}/retry",
    responses={400: {"description": "Redis unavailable or job retry failed"}},
)
def retry_failed_job(job_id: str, user: AdminUser):
    """Re-queue a failed job."""
    from app.core.redis import get_redis_raw, is_redis_available

    if not is_redis_available():
        raise HTTPException(400, "Redis not available")

    r = get_redis_raw()
    try:
        from rq.job import Job

        job = Job.fetch(job_id, connection=r)
        job.requeue()
        return {"status": "requeued", "job_id": job_id}
    except Exception as e:
        raise HTTPException(400, f"Failed to retry job: {e}")


@app.delete(
    f"{_API_PREFIX}/admin/failed-jobs/{{job_id}}",
    responses={400: {"description": "Redis unavailable or job deletion failed"}},
)
def delete_failed_job(job_id: str, user: AdminUser):
    """Delete a failed job permanently."""
    from app.core.redis import get_redis_raw, is_redis_available

    if not is_redis_available():
        raise HTTPException(400, "Redis not available")

    r = get_redis_raw()
    try:
        from rq.job import Job

        job = Job.fetch(job_id, connection=r)
        job.delete()
        return {"status": "deleted", "job_id": job_id}
    except Exception as e:
        raise HTTPException(400, f"Failed to delete job: {e}")
