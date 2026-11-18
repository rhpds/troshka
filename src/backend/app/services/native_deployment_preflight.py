"""A DB fence arbitrates local recovery against the first project I/O handoff.

Only supported fresh project API constructors record a clean baseline. Historical rows,
missing intents and negative provider reads do not establish one.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.native_deployment_preflight import (
    NativeDeploymentCleanBaseline as Baseline,
)
from app.models.native_deployment_preflight import (
    NativeDeploymentPreflight as Attempt,
)
from app.models.native_deployment_preflight import (
    NativeDeploymentPreflightActive as Active,
)
from app.models.native_deployment_preflight import (
    NativeDeploymentPreflightDecision as Decision,
)
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest
from app.services.quota_locks import lock_project

worker_attempt: ContextVar[str | None] = ContextVar(
    "native_preflight_attempt", default=None
)
worker_session: ContextVar[Session | None] = ContextVar(
    "native_preflight_worker_session", default=None
)


def _require(condition, message):
    if not condition:
        raise AccountingUnavailable(message)


def _payload(row):
    _require(
        row is not None and digest(row.payload) == row.digest,
        "Native preflight proof missing or changed",
    )
    if TYPE_CHECKING:
        assert row is not None
    return row.payload


def record_fresh_project(db, project):
    """Call only in the transaction inserting a fresh API project."""
    _require(project in db.new, "Clean baseline requires fresh project insertion")
    db.flush()
    value = dict(
        project_id=project.id, owner_id=project.owner_id, source="fresh_project_create"
    )
    db.add(Baseline(project_id=project.id, digest=digest(value), payload=value))


def current(db, project_id):
    pointer = (
        db.query(Active).filter_by(project_id=project_id).populate_existing().first()
    )
    return pointer.attempt_id if pointer else None


def _previous_baseline(db, project, owner):
    from app.services import native_deployment_origin as origin

    previous = current(db, project.id)
    baseline = db.get(Baseline, project.id)
    if baseline:
        _require(
            _payload(baseline)["owner_id"] == owner.id,
            "Native clean baseline owner changed",
        )
    if previous is None:
        return baseline.digest if baseline else None
    previous_value = _payload(db.get(Attempt, previous))
    if previous_value["mode"] == "unsupported":
        return None
    decision = db.get(Decision, previous)
    _require(
        decision is not None, "Original native deployment attempt is still retained"
    )
    if _payload(decision)["kind"] == "closed_no_project_io":
        return previous_value["baseline_digest"]
    _require(
        origin.active(db, project.id) is None,
        "Original native origin is still retained",
    )
    # A settled origin may deploy again, but absence of its pointer does not
    # create trusted clean-baseline authority for the new generation.
    return None


def begin(db, project, *, supported=True):
    from app.models.host import Host
    from app.services import native_deployment_origin as origin
    from app.services.quota_accounting import quotas_enabled

    project, owner, _ = lock_project(db, project.id, require_organization=False)
    host = db.get(Host, project.host_id) if project.host_id else None
    participating = (
        supported
        and host is not None
        and host.host_type == "kubevirt-cluster"
        and origin.capture_enabled()
        and not quotas_enabled()
    )
    baseline_digest = _previous_baseline(db, project, owner)
    value = dict(
        id=str(uuid4()),
        project_id=project.id,
        owner_id=owner.id,
        host_id=host.id if host else None,
        provider_id=host.provider_id if host else None,
        mode="origin" if participating else "unsupported",
        baseline_digest=baseline_digest if participating else None,
    )
    db.add(
        Attempt(
            id=value["id"], project_id=project.id, digest=digest(value), payload=value
        )
    )
    db.flush()
    pointer = db.get(Active, project.id)
    if pointer:
        pointer.attempt_id = value["id"]
    else:
        db.add(Active(project_id=project.id, attempt_id=value["id"]))
    db.flush()
    return value["id"]


def enter_worker(project_id, attempt_id):
    """Uninstrumented dispatch permanently invalidates pristine recovery proof."""
    from app.core.database import SessionLocal
    from app.models.project import Project

    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            return None
        if attempt_id is None:
            previous = current(db, project_id)
            if previous:
                _require(
                    _payload(db.get(Attempt, previous))["mode"] == "unsupported",
                    "Native deployment requires explicit original worker attempt",
                )
            begin(db, project, supported=False)
            db.commit()
            return None
        value = _payload(db.get(Attempt, attempt_id))
        if value["mode"] != "origin":
            return None
        check(db, project_id, attempt_id)
        return attempt_id


def prepare_dispatch(db, project):
    attempt_id = begin(db, project)
    db.commit()
    return attempt_id


def check(db, project_id, attempt_id):
    project, owner, _ = lock_project(db, project_id, require_organization=False)
    _require(current(db, project_id) == attempt_id, "Native preflight attempt changed")
    attempt = db.get(Attempt, attempt_id)
    value = _payload(attempt)
    from app.models.host import Host

    host = db.get(Host, project.host_id) if project.host_id else None
    _require(value["owner_id"] == owner.id, "Native preflight owner changed")
    if value["mode"] == "origin":
        _require(
            host is not None
            and (value["host_id"], value["provider_id"]) == (host.id, host.provider_id),
            "Native preflight deployment identity changed",
        )
    decision = db.get(Decision, attempt_id)
    _require(
        decision is None or _payload(decision)["kind"] == "handed_to_origin",
        "Native preflight attempt is closed",
    )
    return project, attempt


def _decide(db, attempt, kind):
    value = dict(attempt_id=attempt.id, attempt_digest=attempt.digest, kind=kind)
    db.add(Decision(attempt_id=attempt.id, digest=digest(value), payload=value))
    db.flush()


def handoff(db, project_id):
    """Shares the caller's locked transaction with origin.prepare, before writes."""
    attempt_id = worker_attempt.get()
    if attempt_id is None:
        project, _, _ = lock_project(db, project_id, require_organization=False)
        pointer = current(db, project.id)
        _require(
            pointer is None
            or _payload(db.get(Attempt, pointer))["mode"] == "unsupported",
            "Native deployment requires explicit worker attempt",
        )
        return
    _, attempt = check(db, project_id, attempt_id)
    if db.get(Decision, attempt_id) is None:
        _decide(db, attempt, "handed_to_origin")


def request(db, project, mode):
    attempt_id = current(db, project.id)
    if attempt_id is None:
        return None
    decision = db.get(Decision, attempt_id)
    if decision is not None:
        return None
    value = _payload(db.get(Attempt, attempt_id))
    if value["mode"] != "origin":
        return None
    project, attempt = check(db, project.id, attempt_id)
    if db.get(Decision, attempt_id) is not None:
        return None
    value = _payload(attempt)
    baseline = db.get(Baseline, project.id)
    _require(
        baseline is not None and value["baseline_digest"] == baseline.digest,
        "Native early failure has no trusted clean baseline",
    )
    _require(
        _payload(baseline)["owner_id"] == project.owner_id,
        "Native clean baseline owner changed",
    )
    from app.services import native_deployment_origin as origin

    _require(
        origin.active(db, project.id) is None,
        "Native origin already owns this deployment",
    )
    _require(mode in ("undeploy", "delete"), "Unsupported native cleanup mode")
    _decide(db, attempt, "closed_no_project_io")
    if mode == "delete":
        db.delete(project)
        result = dict(status="deleted", id=project.id)
    else:
        project.state, project.host_id, project.vni_map, project.deploy_error = (
            "draft",
            None,
            None,
            None,
        )
        project.deployed_topology = None
        result = dict(status="draft")
    db.commit()
    return result


def guard_worker_session(db, project_id, attempt_id):
    """Fence all delayed worker ORM writes using the same canonical DB lock."""

    def guard(session, _context, _instances):
        if session.new or session.dirty or session.deleted:
            # Core locks deliberately avoid populate_existing on dirty ORM rows.
            from sqlalchemy import select

            from app.models.managed_group import ManagedGroup
            from app.models.project import Project
            from app.models.user import User

            connection = session.connection()
            identity = connection.execute(
                select(Project.owner_id, User.organization_id)
                .join(User, Project.owner_id == User.id)
                .where(Project.id == project_id)
            ).first()
            _require(identity is not None, "Native preflight project no longer exists")
            for table, key in (
                (User.__table__, identity[0]),
                (ManagedGroup.__table__, identity[1]),
                (Project.__table__, project_id),
            ):
                if key:
                    connection.execute(
                        select(table.c.id).where(table.c.id == key).with_for_update()
                    ).all()
            pointer = connection.execute(
                select(Active.attempt_id).where(Active.project_id == project_id)
            ).scalar()
            decision = connection.execute(
                select(Decision.payload).where(Decision.attempt_id == attempt_id)
            ).scalar()
            _require(
                pointer == attempt_id
                and (decision is None or decision["kind"] == "handed_to_origin"),
                "Native deployment worker is stale or closed",
            )
            from app.models.native_deployment_origin import NativeDeploymentOriginActive

            cancelled = connection.execute(
                select(NativeDeploymentOriginActive.cancelled).where(
                    NativeDeploymentOriginActive.project_id == project_id
                )
            ).scalar()
            _require(not cancelled, "Native deployment origin was cancelled")

    event.listen(db, "before_flush", guard)


@contextmanager
def _worker_database():
    """Borrow the worker transaction instead of locking it from another session."""
    existing = worker_session.get()
    if existing is not None:
        yield existing
        return
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        yield db


def _check_worker(db, project_id, attempt_id):
    # Flush the participating session guard before check() can refresh Project.
    # This preserves intended changes and uses the same locked connection.
    db.flush()
    return check(db, project_id, attempt_id)


def worker_allowed(project_id):
    attempt_id = worker_attempt.get()
    if attempt_id is None:
        return True
    with _worker_database() as db:
        try:
            if not db.is_active:
                return False
            _check_worker(db, project_id, attempt_id)
            return True
        except AccountingUnavailable:
            return False


def progress(project_id, data, *, notify=False):
    """Keep shared progress updates inside the closure/handoff serialization."""
    from app.core.redis import delete_progress, set_progress
    from app.services.ws_pubsub import notify_project

    with _worker_database() as db:
        try:
            project, _ = _check_worker(db, project_id, worker_attempt.get())
        except AccountingUnavailable:
            return
        if data is None:
            delete_progress(f"deploy:{project_id}")
        else:
            set_progress(f"deploy:{project_id}", data)
        if notify:
            project.deploy_progress = data
            notify_project(project_id, {"type": "deploy-progress", "progress": data})
        db.commit()


def fail_job(
    project_id, attempt_id, exc_value, connection, *, host_id=None, abandoned=False
):
    from app.core.database import SessionLocal
    from app.core.redis import delete_progress

    with SessionLocal() as db:
        try:
            project, _ = check(db, project_id, attempt_id)
        except AccountingUnavailable:
            return
        from app.core.redis import _cleanup_host_locks

        _cleanup_host_locks(host_id, project_id)
        if abandoned:
            return
        if project.state == "deploying":
            project.state = "error"
            project.deploy_error = (
                str(exc_value) or "Job failed unexpectedly. Please retry."
            )
        delete_progress(f"deploy:{project_id}")
        connection.delete(f"job:project:{project_id}")
        db.commit()


def finish_job(project_id, attempt_id, connection, *, host_id=None):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        try:
            check(db, project_id, attempt_id)
        except AccountingUnavailable:
            return
        if host_id:
            from app.services.placement import record_deploy_end

            record_deploy_end(host_id)
        connection.delete(f"job:project:{project_id}")
        db.commit()
