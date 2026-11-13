import datetime
import logging
import threading
import time

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 30
_WARNING_MINUTES = 5
_TRANSITIONAL_STATES = frozenset(
    ("deploying", "stopping", "starting", "reconfiguring", "migrating")
)


def _timer_quota_preflight(s, project, action):
    """A known unsupported native action must not starve unrelated timers."""
    from fastapi import HTTPException

    from app.services.quota_cleanup import guard_supported_cleanup
    from app.services.quota_enforcement import request_native_stop

    try:
        if action == "stop":
            action_id = request_native_stop(s, project, project.off_action or "stop")
            if action_id:
                return action_id
        else:
            from app.services.quota_cleanup import request_native

            pending = request_native(s, project, "delete")
            if pending is not None:
                return "cleanup_pending"
            guard_supported_cleanup(s, project)
    except HTTPException as error:
        if error.status_code != 409:
            raise
        s.rollback()
        logger.warning(
            "Auto-%s unavailable for project %s; assets retained", action, project.id
        )
        return False
    return True


def _process_expired_timers(s, now, result, _dry_run):
    """Handle expired auto-stop and auto-delete timers."""
    from app.models.project import Project

    # Expired auto-stop
    expired_stop = (
        s.query(Project)
        .filter(
            Project.auto_stop_expires_at <= now,
            Project.state == "active",
        )
        .all()
    )
    for p in expired_stop:
        result["auto_stop"].append(p.id)
        if _dry_run:
            continue
        logger.info("Auto-stop fired for project %s (%s)", p.name, p.id[:8])
        stop_action = _timer_quota_preflight(s, p, "stop")
        if not stop_action:
            continue
        p.state = "stopping"
        p.auto_stopped = True
        p.auto_stop_started_at = None
        p.auto_stop_expires_at = None
        p.auto_stop_warned = False
        s.commit()
        _notify(p.id, {"type": "timer_fired", "timer": "auto_stop"})
        _queue_timer_stop(p.id, stop_action)

    # Expired auto-delete
    expired_delete = (
        s.query(Project)
        .filter(
            Project.lifetime_expires_at <= now,
            Project.state.in_(("active", "stopped", "error", "draft")),
        )
        .all()
    )
    for p in expired_delete:
        if p.state in _TRANSITIONAL_STATES:
            continue
        result["auto_delete"].append(p.id)
        if _dry_run:
            continue
        delete_action = _timer_quota_preflight(s, p, "delete")
        if not delete_action or delete_action == "cleanup_pending":
            continue
        logger.info("Auto-delete fired for project %s (%s)", p.name, p.id[:8])
        _notify(p.id, {"type": "timer_fired", "timer": "auto_delete"})
        _delete_project(s, p)


def _process_timer_warnings(s, now, warning_threshold, result, _dry_run):
    """Send warnings for timers about to expire."""
    from app.models.project import Project

    # Auto-stop warning
    warn_stop = (
        s.query(Project)
        .filter(
            Project.auto_stop_expires_at <= warning_threshold,
            Project.auto_stop_expires_at > now,
            Project.auto_stop_warned == False,  # noqa: E712
            Project.state == "active",
        )
        .all()
    )
    for p in warn_stop:
        result["auto_stop_warned"].append(p.id)
        if _dry_run:
            continue
        if not p.auto_stop_expires_at:
            continue
        remaining = (p.auto_stop_expires_at - now).total_seconds() / 60
        p.auto_stop_warned = True
        s.commit()
        _notify(
            p.id,
            {
                "type": "timer_warning",
                "timer": "auto_stop",
                "expires_at": p.auto_stop_expires_at.isoformat(),
                "minutes_remaining": round(remaining),
            },
        )

    # Auto-delete warning
    warn_delete = (
        s.query(Project)
        .filter(
            Project.lifetime_expires_at <= warning_threshold,
            Project.lifetime_expires_at > now,
            Project.auto_delete_warned == False,  # noqa: E712
            Project.state.notin_(_TRANSITIONAL_STATES),
        )
        .all()
    )
    for p in warn_delete:
        result["auto_delete_warned"].append(p.id)
        if _dry_run:
            continue
        if not p.lifetime_expires_at:
            continue
        remaining = (p.lifetime_expires_at - now).total_seconds() / 60
        p.auto_delete_warned = True
        s.commit()
        _notify(
            p.id,
            {
                "type": "timer_warning",
                "timer": "auto_delete",
                "expires_at": p.lifetime_expires_at.isoformat(),
                "minutes_remaining": round(remaining),
            },
        )


def _started_deploy_is_zombie(status: str, project, get_progress) -> bool:
    return (
        status == "started"
        and project.state == "deploying"
        and not get_progress(f"deploy:{project.id}")
    )


def _mark_project_error(s, project, job_info, clear_job_ref, result) -> None:
    logger.warning(
        "Recovering stuck project %s (%s) — state=%s, no active job",
        project.name,
        project.id[:8],
        project.state,
    )
    result.setdefault("stuck_recovered", []).append(project.id)
    old_state = project.state
    project.state = "error"
    project.deploy_error = f"Background job lost while {old_state} — please retry"
    if job_info and job_info.get("job_id"):
        clear_job_ref(project.id, job_info.get("job_id"))
    s.commit()
    _notify(
        project.id,
        {
            "type": "project-state",
            "state": "error",
            "deploy_error": project.deploy_error,
        },
    )


def _recover_one_stuck_project(
    s, project, result, dry_run, *, get_job_info, get_progress, clear_job_ref
) -> None:
    from app.models.quota import QuotaAction
    from app.services.native_managed_deploy import has_pending_handoff

    if project.state == "stopping":
        run = (
            s.query(QuotaAction)
            .filter_by(project_id=project.id, kind="stop_run")
            .first()
        )
        if (
            run
            and run.state in {"pending", "issued", "failed"}
            and run.details.get("managed_context")
        ):
            return  # Durable managed observation/retry is the active job.
    job_info = get_job_info(project.id)
    status = (job_info or {}).get("status") or ""
    if has_pending_handoff(s, project, status):
        return  # The completed worker handed ownership to durable reconciliation.
    if status in ("queued", "started"):
        if _started_deploy_is_zombie(status, project, get_progress):
            logger.warning(
                "Project %s (%s) has started job with no deploy progress — treating as lost",
                project.name,
                project.id[:8],
            )
            if not dry_run:
                clear_job_ref(project.id, (job_info or {}).get("job_id"))
        else:
            return
    if dry_run:
        result.setdefault("stuck_recovered", []).append(project.id)
        logger.warning(
            "Recovering stuck project %s (%s) — state=%s, no active job",
            project.name,
            project.id[:8],
            project.state,
        )
        return
    _mark_project_error(s, project, job_info, clear_job_ref, result)


def _recover_stuck_projects(s, now, result, dry_run):
    """Recover projects stuck in transitional states with no active RQ job."""
    from app.core.redis import (
        _clear_project_job_ref,
        get_job_info,
        get_progress,
        is_redis_available,
    )
    from app.models.project import Project

    if not is_redis_available():
        return

    grace = now - datetime.timedelta(minutes=5)
    stuck = (
        s.query(Project)
        .filter(
            Project.state.in_(("deploying", "starting", "stopping", "reconfiguring")),
            Project.updated_at < grace,
        )
        .all()
    )
    for p in stuck:
        _recover_one_stuck_project(
            s,
            p,
            result,
            dry_run,
            get_job_info=get_job_info,
            get_progress=get_progress,
            clear_job_ref=_clear_project_job_ref,
        )


def _check_project_timers(_dry_run=False):
    from app.core.database import SessionLocal

    result = {
        "auto_stop": [],
        "auto_delete": [],
        "auto_stop_warned": [],
        "auto_delete_warned": [],
    }

    s = SessionLocal()
    try:
        now = datetime.datetime.now(datetime.UTC)
        warning_threshold = now + datetime.timedelta(minutes=_WARNING_MINUTES)
        _process_expired_timers(s, now, result, _dry_run)
        _process_timer_warnings(s, now, warning_threshold, result, _dry_run)
        _recover_stuck_projects(s, now, result, _dry_run)
    except Exception:
        logger.exception("Project timer check error")
        s.rollback()
    finally:
        s.close()

    return result


def _notify(project_id, message):
    try:
        from app.services.ws_pubsub import notify_project

        notify_project(project_id, message)
    except Exception:
        logger.warning("Failed to send timer notification for %s", project_id[:8])


def _queue_timer_stop(project_id, action):
    if isinstance(action, str):
        _spawn_stop(project_id, action)
    else:
        _spawn_stop(project_id)


def _spawn_stop(project_id, action_id=None):
    from app.core.redis import enqueue_job
    from app.services.deploy_service import stop_project_async

    if action_id:
        from app.services.quota_enforcement import reconcile_worker

        enqueue_job(reconcile_worker, action_id)
    else:
        enqueue_job(stop_project_async, project_id, project_id=project_id)


def _delete_project(s, project):
    import copy

    from app.services.quota_cleanup import guard_supported_cleanup, request_native

    if request_native(s, project, "delete") is not None:
        return
    guard_supported_cleanup(s, project)

    from app.services.deploy_service import (
        destroy_project_sync,
        stop_project_async,
    )

    project_id = project.id

    if project.state == "active":
        project.state = "stopping"
        s.commit()
        stop_project_async(project_id)
        s.refresh(project)

    from app.services.quota_accounting import retain_attributed_history

    retain_attributed_history(s, [project.id], datetime.datetime.now(datetime.UTC))
    _notify(project_id, {"type": "project-deleted"})

    if project.host_id and project.state in ("stopped", "error"):
        destroy_ctx = {
            "project_id": project.id,
            "host_id": project.host_id,
            "vni_map": copy.deepcopy(project.vni_map or {}),
            "topology": copy.deepcopy(
                project.deployed_topology or project.topology or {}
            ),
            "dns_provider_id": project.dns_provider_id,
            "domain": project.domain,
        }
        from app.core.redis import enqueue_job

        enqueue_job(destroy_project_sync, destroy_ctx, project_id=project_id)

    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import release_eip

    project_eips = s.query(ElasticIp).filter_by(project_id=project_id).all()
    for eip in project_eips:
        try:
            release_eip(s, eip)
        except Exception:
            logger.warning("Failed to release EIP %s on timer delete", eip.public_ip)

    # EIP release commits this session; reacquire identities after its last commit.
    retain_attributed_history(s, [project.id], datetime.datetime.now(datetime.UTC))
    s.delete(project)
    s.commit()
    logger.info("Auto-delete complete for project %s", project_id[:8])


def _timer_loop():
    logger.info("Project timer started (interval=%ds)", _INTERVAL_SECONDS)
    while True:
        time.sleep(_INTERVAL_SECONDS)
        try:
            _check_project_timers()
        except Exception:
            logger.exception("Project timer loop error")


def start_project_timer():
    thread = threading.Thread(target=_timer_loop, daemon=True, name="project-timer")
    thread.start()
    return thread
