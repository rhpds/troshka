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


def _check_project_timers(_dry_run=False):
    from app.core.database import SessionLocal
    from app.models.project import Project

    result = {
        "auto_stop": [],
        "auto_delete": [],
        "auto_stop_warned": [],
        "auto_delete_warned": [],
    }

    s = SessionLocal()
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        warning_threshold = now + datetime.timedelta(minutes=_WARNING_MINUTES)

        # 1. Expired auto-stop
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

        # 2. Expired auto-delete
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

        # 3. Auto-stop warning
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

        # 4. Auto-delete warning
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
    import threading

    from app.services.deploy_service import stop_project_async

    threading.Thread(
        target=stop_project_async,
        args=(project_id,),
        daemon=True,
        name=f"timer-stop-{project_id[:8]}",
    ).start()


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
        import threading

        threading.Thread(
            target=destroy_project_sync,
            args=(destroy_ctx,),
            daemon=True,
            name=f"timer-destroy-{project_id[:8]}",
        ).start()

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
