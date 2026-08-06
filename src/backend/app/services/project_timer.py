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
        p.state = "stopping"
        p.auto_stopped = True
        p.auto_stop_started_at = None
        p.auto_stop_expires_at = None
        p.auto_stop_warned = False
        s.commit()
        _notify(p.id, {"type": "timer_fired", "timer": "auto_stop"})
        _spawn_stop(p.id)

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


def _recover_stuck_projects(s, now, result, _dry_run):
    """Recover projects stuck in transitional states with no active RQ job."""
    from app.core.redis import get_job_info, is_redis_available
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
        job_info = get_job_info(p.id)
        if job_info and job_info.get("status") in ("queued", "started"):
            continue
        logger.warning(
            "Recovering stuck project %s (%s) — state=%s, no active job",
            p.name,
            p.id[:8],
            p.state,
        )
        result.setdefault("stuck_recovered", []).append(p.id)
        if _dry_run:
            continue
        old_state = p.state
        p.state = "error"
        p.deploy_error = f"Background job lost while {old_state} — please retry"
        s.commit()
        _notify(
            p.id,
            {
                "type": "project-state",
                "state": "error",
                "deploy_error": p.deploy_error,
            },
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


def _spawn_stop(project_id):
    from app.core.redis import enqueue_job
    from app.services.deploy_service import stop_project_async

    enqueue_job(stop_project_async, project_id, project_id=project_id)


def _delete_project(s, project):
    import copy

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
