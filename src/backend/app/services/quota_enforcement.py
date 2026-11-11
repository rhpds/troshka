"""Durable calculated-spending enforcement; never a resource destruction path.

evaluate owns no commits. reconcile_action owns commit/rollback and must receive
a clean session: it commits its claim before provider I/O. A separate autocommit
session advisory mutex prevents concurrent/live stop executions across processes;
no database transaction/row lock spans a provider call. Lost processes release
that mutex, leaving the persisted action available for observation/retry.
"""

import logging
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import text

from app.models.project import Project
from app.models.quota import QuotaAction, QuotaPolicy, QuotaReadiness
from app.models.user import User
from app.schemas.quota import Resources, Scope
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_operations as operations
from app.services.quota_locks import ScopeChanged, lock_rows
from app.services.quota_policy import get_limits

logger = logging.getLogger(__name__)
_mutexes = {}
_mutexes_guard = threading.Lock()
_poll_started = False


def _now():
    return datetime.now(UTC)


def _snapshot(db, project_id=None):
    query = db.query(Project.id, Project.owner_id, User.organization_id).join(
        User, Project.owner_id == User.id
    )
    if project_id:
        owner = query.filter(Project.id == project_id).first()
        if not owner:
            return []
        query = query.filter(
            (User.organization_id == owner.organization_id)
            | (Project.owner_id == owner.owner_id)
        )
    return query.order_by(Project.id).all()


def _lock_inventory(db, project_id=None):
    """Lock a stable spend closure in global order, without later user locks.

    Full periodic evaluation takes the full live inventory once. One action only
    locks its current owner's organization and that organization's project closure."""
    db.flush()
    before = _snapshot(db, project_id)
    lock_rows(
        db,
        user_ids={r.owner_id for r in before}
        if project_id is None
        else {r.owner_id for r in before if r.id == project_id},
        organization_ids={r.organization_id for r in before if r.organization_id},
        project_ids={r.id for r in before},
    )
    if before != _snapshot(db, project_id):
        raise ScopeChanged("Budget evaluation membership changed; rollback and retry")
    return [db.get(Project, row.id) for row in before]


def _scopes(db, project) -> list[tuple[Scope, str]]:
    owner = db.query(User).filter_by(id=project.owner_id).populate_existing().first()
    result: list[tuple[Scope, str]] = [("project", project.id)]
    if owner:
        result.append(("user", owner.id))
        if owner.organization_id:
            result.append(("organization", owner.organization_id))
    return result


def _period(scope, now):
    return "lifetime" if scope == "project" else accounting.utc(now).strftime("%Y-%m")


def _policy(db, scope, scope_id, now):
    cap = get_limits(db, scope, scope_id).spending_usd
    policy = (
        db.query(QuotaPolicy)
        .filter_by(scope=scope, scope_id=scope_id)
        .populate_existing()
        .first()
    )
    revision = policy.revision if policy else 0
    return cap, revision, _period(scope, now)


def _action(db, scope, scope_id, revision, period, kind, threshold, now, project_id=""):
    identity = dict(
        scope=scope,
        scope_id=scope_id,
        policy_revision=revision,
        period_key=period,
        kind=kind,
        threshold=threshold,
        project_id=project_id,
    )
    row = db.query(QuotaAction).filter_by(**identity).populate_existing().first()
    if row is None:
        row = QuotaAction(
            **identity, state="pending", attempts=0, updated_at=now, details={}
        )
        db.add(row)
        db.flush()
    return row


def _status_row(db, project_id):
    key = str(uuid5(NAMESPACE_URL, f"troshka:enforcement:{project_id}"))
    row = db.get(QuotaReadiness, key)
    if row is None:
        row = QuotaReadiness(id=key, details={})
        db.add(row)
    return row


def _cause_key(row):
    return row.scope, row.scope_id, row.policy_revision, row.period_key


def _spending_causes(db, project, now, cache):
    current, unavailable = set(), False
    for scope, identity in _scopes(db, project):
        try:
            key = scope, identity
            if key not in cache:
                cache[key] = _scope_spend(db, scope, identity, now)
            cap, revision, period, spend = cache[key]
            if cap is not None:
                _thresholds(
                    db,
                    project,
                    scope,
                    identity,
                    cap,
                    revision,
                    period,
                    spend,
                    now,
                    current,
                )
        except accounting.AccountingUnavailable:
            unavailable = True
            proof = _retained_proof(db, project.id, scope, identity, now)
            if proof:
                current.add(_cause_key(proof))
    return current, unavailable


def _evaluate_project(db, project, now, cache):
    current, unavailable = _spending_causes(db, project, now, cache)
    spending_exhausted = bool(current)
    storage_causes, storage_unavailable = _storage_causes(db, project, now)
    current.update(storage_causes)
    unavailable = unavailable or storage_unavailable
    _cancel_obsolete(db, project.id, current, now)
    marker = _status_row(db, project.id)
    marker.ready = not unavailable
    marker.checked_at = now
    marker.details = {
        "accounting_status": "unavailable" if unavailable else "ready",
        "spending_state": "budget_exceeded_shutdown_pending"
        if spending_exhausted
        else ("accounting_unavailable" if unavailable else "within_budget"),
        "storage_state": "storage_exceeded_shutdown_pending"
        if storage_causes
        else ("storage_unavailable" if storage_unavailable else "within_allowance"),
        "cause_ids": [
            row.id for row in _stops(db, project.id) if _cause_key(row) in current
        ],
    }
    if current:
        _ensure_run(db, project, marker.details["cause_ids"], now)
    return current, unavailable


def _storage_causes(db, project, now):
    from app.services import quota_storage as storage

    current, unavailable = set(), False
    for scope, identity in _scopes(db, project):
        limit = get_limits(db, scope, identity).storage_bytes
        policy = db.query(QuotaPolicy).filter_by(scope=scope, scope_id=identity).first()
        revision = policy.revision if policy else 0
        try:
            storage.scope_evidence(db, scope, identity, now)
            used, _reserved = admission.usage(db, scope, identity)
            if limit is not None and used.storage_bytes >= limit:
                cause = _action(
                    db,
                    scope,
                    identity,
                    revision,
                    "storage",
                    "stop",
                    100,
                    now,
                    project.id,
                )
                cause.details = {
                    "reason": "physical_storage_limit",
                    "used_bytes": used.storage_bytes,
                    "limit_bytes": limit,
                }
                if cause.state == "cancelled":
                    cause.state = "pending"
                current.add(_cause_key(cause))
        except accounting.AccountingUnavailable:
            unavailable = True
            _retain_storage_cause(
                db, project.id, scope, identity, revision, "storage", limit, current
            )
    pool_current, pool_unavailable = _pool_causes(db, project, now)
    return current | pool_current, unavailable or pool_unavailable


def _retain_storage_cause(
    db, project_id, scope, identity, revision, period, limit, current
):
    if limit is None:
        return
    for cause in _stops(db, project_id):
        if (cause.scope, cause.scope_id, cause.policy_revision, cause.period_key) == (
            scope,
            identity,
            revision,
            period,
        ):
            if cause.state != "cancelled" and cause.details.get("limit_bytes") == limit:
                current.add(_cause_key(cause))


def _pool_causes(db, project, now):
    from app.services import quota_storage as storage

    rows = operations.current_generation(operations._rows(db, project.id))
    if not rows or not any(any(row.observed.values()) for row in rows):
        return set(), False
    try:
        root = storage.measurement_row(rows)
        evidence = root.dispatch_metadata.get("storage_evidence", {})
        storage.validate(
            evidence, project.id, operations._generation(root), project.host_id, now
        )
        pool = evidence["pools"][0]
        if pool["free_bytes"] > storage.reserve_floor(pool):
            return set(), False
        cause = _action(
            db, "project", project.id, 0, "physical-pool", "stop", 100, now, project.id
        )
        cause.details = {
            "reason": "physical_pool_headroom",
            "pool_id": pool["id"],
            "host_id": project.host_id,
            "free_bytes": pool["free_bytes"],
            "reserve_bytes": storage.reserve_floor(pool),
        }
        if cause.state == "cancelled":
            cause.state = "pending"
        return {_cause_key(cause)}, False
    except accounting.AccountingUnavailable:
        retained = {
            _cause_key(cause)
            for cause in _stops(db, project.id)
            if cause.period_key == "physical-pool"
            and cause.state != "cancelled"
            and cause.details.get("host_id") == project.host_id
        }
        return retained, True


def _scope_spend(db, scope, identity, now):
    cap, revision, period = _policy(db, scope, identity, now)
    spend = accounting.spend(db, scope, identity, now) if cap is not None else None
    return cap, revision, period, spend


def _retained_proof(db, project_id, scope, identity, now):
    """Charges cannot decrease within this exact scope/revision/period/cap.

    An unavailable new observation alone never creates exhaustion. Previously proven
    exhaustion survives a failed stop's error state, but never a new policy, month or
    membership. Calling scopes are resolved from current authoritative membership."""
    cap, revision, period = _policy(db, scope, identity, now)
    row = (
        db.query(QuotaAction)
        .filter_by(
            scope=scope,
            scope_id=identity,
            policy_revision=revision,
            period_key=period,
            kind="stop",
            threshold=100,
            project_id=project_id,
        )
        .populate_existing()
        .first()
    )
    if (
        cap is None
        or not row
        or "spent_usd" not in row.details
        or "limit_usd" not in row.details
    ):
        return None
    if (
        Decimal(row.details["limit_usd"]) == cap
        and Decimal(row.details["spent_usd"]) >= cap
    ):
        return row
    return None


def _ensure_run(db, project, cause_ids, now):
    run = _action(
        db, "project", project.id, 0, "current", "stop_run", 100, now, project.id
    )
    changed = set(run.details.get("cause_ids", [])) != set(cause_ids)
    if run.state in {"completed", "cancelled"} and (
        changed or _has_compute(db, project.id)
    ):
        run.state, run.next_attempt_at = "pending", None
        run.details = {}


def _has_compute(db, project_id):
    return any(
        (
            row.observed.get("cpu")
            or row.observed.get("ram_bytes")
            or (
                row.state in admission.PENDING
                and not row.dispatch_metadata.get("cancelled")
            )
        )
        for row in operations._rows(db, project_id)
    )


def _thresholds(
    db, project, scope, identity, cap, revision, period, spend, now, current
):
    for threshold in (80, 90):
        if cap > 0 and spend * 100 >= cap * threshold:
            warning = _action(
                db, scope, identity, revision, period, "warning", threshold, now
            )
            warning.details = {
                **warning.details,
                "spent_usd": str(spend),
                "limit_usd": str(cap),
            }
    if spend >= cap:
        cause = _action(
            db, scope, identity, revision, period, "stop", 100, now, project.id
        )
        cause.details = {"spent_usd": str(spend), "limit_usd": str(cap)}
        if cause.state == "cancelled":
            cause.state = "pending"
        current.add(_cause_key(cause))


def _stops(db, project_id):
    return (
        db.query(QuotaAction)
        .filter_by(project_id=project_id, kind="stop")
        .populate_existing()
        .all()
    )


def _cancel_obsolete(db, project_id, current, now):
    for cause in _stops(db, project_id):
        if _cause_key(cause) not in current and cause.state != "completed":
            cause.state, cause.updated_at = "cancelled", now


def evaluate(db, now):
    """Accrue current spend and persist threshold/cause intents; caller commits.

    No dispatch or notifications occur under these locks. An accounting outage does
    not become a zero total or new shutdown cause. Every periodic pass processes the
    current policies, including durable transfer/policy reevaluation handoffs."""
    if not accounting.quotas_enabled():
        return
    cache = {}
    for project in _lock_inventory(db):
        _evaluate_project(db, project, now, cache)
    for marker in db.query(QuotaReadiness).all():
        if (marker.details or {}).get("reason") == "spending_policy_changed":
            marker.ready = True
            marker.checked_at = now
    db.flush()


def project_status(db, project_id):
    marker = _status_row(db, project_id)
    run = (
        db.query(QuotaAction).filter_by(project_id=project_id, kind="stop_run").first()
    )
    cause_ids = set((run.details or {}).get("cause_ids", [])) if run else set()
    reasons = {
        cause.details.get("reason", "spending_limit")
        for cause in _stops(db, project_id)
        if cause.id in cause_ids or cause.id in marker.details.get("cause_ids", [])
    }
    return {
        **marker.details,
        "enforcement_enabled": accounting.quotas_enabled(),
        "action_reasons": sorted(reasons),
        "action_state": run.state if run else None,
        "attempts": run.attempts if run else 0,
        "next_attempt_at": run.next_attempt_at.isoformat()
        if run and run.next_attempt_at
        else None,
        "last_error": run.last_error if run else None,
    }


@contextmanager
def _execution_guard(db, project_id):
    """Session mutex, acquired with no identity transaction held; never pooled locked."""
    engine = db.get_bind()
    key = int.from_bytes(
        sha256(f"quota-stop:{project_id}".encode()).digest()[:8], signed=True
    )
    if engine.dialect.name != "postgresql":
        with _mutexes_guard:
            mutex = _mutexes.setdefault(project_id, threading.Lock())
        acquired = mutex.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                mutex.release()
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        ).scalar()
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                    )
                except Exception:
                    connection.invalidate()
                    raise


def _lock_action_project(db, project_id):
    previous = operations._rows(db, project_id)
    identities = {row.operation_id for row in previous}
    admission.lock_operations(db, identities)
    _lock_inventory(db, project_id)
    rows = operations._rows(db, project_id)
    if identities != {row.operation_id for row in rows}:
        raise ScopeChanged("Allocation changed before budget stop; rollback and retry")
    return operations.current_generation(rows)


def _fence(rows):
    for row in rows:
        row.dispatch_metadata = {**row.dispatch_metadata, "cancelled": True}


def _finished(row, timestamp):
    metadata = row.dispatch_metadata
    if (
        metadata.get("known_empty")
        and not metadata.get("claim")
        and not metadata.get("calls")
        and not any(row.observed.values())
    ):
        return True  # Never-dispatched fenced work is proven non-creation.
    return operations._observation_follows_worker(row, timestamp)


def _confirm_off(db, project, run, rows, now):
    from app.services.ws_pubsub import get_cached_vm_states

    snapshot = get_cached_vm_states(project.id) or {}
    timestamp = snapshot.get("observed_at")
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    if not isinstance(timestamp, datetime) or not all(
        _finished(row, timestamp) for row in rows
    ):
        return False
    age = (accounting.utc(now) - accounting.utc(timestamp)).total_seconds()
    if not 0 <= age <= 60 or not _after_dispatch(run, timestamp):
        return False
    if operations._whole_project_power(project, snapshot) != "off":
        return False
    _fence(rows)
    for row in rows:
        vector = _off_vector(db, row)
        identity = snapshot.get("observation_id") or timestamp.isoformat()
        admission.confirm(
            db, row.operation_id, vector, f"budget-off:{identity}", timestamp
        )
    return True


def _off_vector(db, row):
    """Only proven non-creation releases storage/slot; stopping never does."""
    from app.models.quota import QuotaAllocation

    metadata = row.dispatch_metadata
    never_created = (
        metadata.get("known_empty")
        and not metadata.get("claim")
        and not metadata.get("calls")
        and not any(row.observed.values())
    )
    if (
        never_created
        and not db.query(QuotaAllocation)
        .filter_by(operation_id=row.operation_id)
        .filter(QuotaAllocation.state != "released")
        .first()
    ):
        return Resources()
    vector = Resources(**(row.observed if row.state == "confirmed" else row.requested))
    if metadata.get("storage_measured_at"):
        vector.storage_bytes = row.observed["storage_bytes"]
    vector.cpu = vector.ram_bytes = 0
    return vector


def _after_dispatch(run, timestamp):
    boundaries = [run.details.get(key) for key in ("issued_at", "stop_finished_at")]
    return all(
        accounting.utc(timestamp) > accounting.utc(datetime.fromisoformat(value))
        for value in boundaries
        if value
    )


def _complete(run, causes, now):
    run.state, run.updated_at, run.last_error = "completed", now, None
    run.next_attempt_at = None
    run.details = {**run.details, "cause_ids": [cause.id for cause in causes]}
    for cause in causes:
        cause.state, cause.updated_at = "completed", now


def _prepare_stop(db, run, now):
    rows = _lock_action_project(db, run.project_id)
    project = db.get(Project, run.project_id)
    if not project:
        run.last_error = (
            "Project metadata missing; retained resources require reconciliation"
        )
        return False
    current, _unavailable = _evaluate_project(db, project, now, {})
    causes = [row for row in _stops(db, project.id) if _cause_key(row) in current]
    if not causes:
        run.state = "cancelled"
        return False
    if _complete_noncreation(db, project, rows, now) or _confirm_off(
        db, project, run, rows, now
    ):
        _complete(run, causes, now)
        return False
    if run.next_attempt_at and accounting.utc(run.next_attempt_at) > accounting.utc(
        now
    ):
        return False
    _fence(rows)
    run.state, run.attempts, run.updated_at = "issued", run.attempts + 1, now
    run.operation_id = f"quota-stop:{uuid4()}"
    run.details = {
        "cause_ids": [row.id for row in causes],
        "issued_at": now.isoformat(),
    }
    run.next_attempt_at = now + timedelta(seconds=_backoff(run.attempts))
    for cause in causes:
        cause.state, cause.operation_id = "issued", None
    return True


def _complete_noncreation(db, project, rows, now):
    from app.models.metering import MeteringInterval
    from app.models.quota import QuotaAllocation

    if project.state != "draft" or project.host_id or project.disks:
        return False
    if any(
        row.dispatch_metadata.get("claim")
        or row.dispatch_metadata.get("calls")
        or any(row.observed.values())
        for row in rows
    ):
        return False
    if (
        db.query(MeteringInterval)
        .filter_by(project_id=project.id, ended_at=None)
        .first()
    ):
        return False
    if (
        db.query(QuotaAllocation)
        .filter_by(project_id=project.id)
        .filter(QuotaAllocation.state != "released")
        .first()
    ):
        return False
    _fence(rows)
    for row in rows:
        admission.confirm(
            db,
            row.operation_id,
            Resources(),
            f"budget-noncreation:{now.isoformat()}",
            now,
        )
    return True


def _backoff(attempts):
    return min(1800, 30 * 2 ** min(attempts - 1, 6))


def _dispatch(project_id):
    from app.services.deploy_service import stop_project_async

    stop_project_async(project_id, force_stop=True)


def reconcile_action(db, action_id, now):
    """Recheck, fence, claim, commit, stop, and await fresh observations.

    Owns transaction boundaries. The persisted coordinator survives queue/process
    loss; retrying a known stop is idempotent, bounded by backoff, and protected by a
    cross-process execution mutex. A stop service return is never completion proof."""
    if not accounting.quotas_enabled():
        return
    action = db.get(QuotaAction, action_id)
    if not action or action.kind != "stop_run":
        return
    project_id = action.project_id
    db.commit()  # Acquire session execution mutex before any transactional locks.
    with _execution_guard(db, project_id) as acquired:
        if acquired:
            _reconcile_locked(db, action_id, now)


def _reconcile_locked(db, action_id, now):
    run = db.query(QuotaAction).filter_by(id=action_id).populate_existing().one()
    if run.state in {"completed", "cancelled"}:
        return
    dispatch = _prepare_stop(db, run, now)
    project_id, token = run.project_id, run.operation_id
    db.commit()
    if not dispatch:
        return
    error = None
    try:
        _dispatch(project_id)
    except Exception:
        logger.exception("Budget stop failed for %s", project_id)
        error = "Compute shutdown failed; awaiting observation and retry"
    # The existing stop service catches provider exceptions internally. Its
    # explicit error state is failure evidence, never successful-stop evidence.
    if (
        error is None
        and db.query(Project.state).filter_by(id=project_id).scalar() == "error"
    ):
        error = "Compute shutdown reported an error; awaiting observation and retry"
    run = (
        db.query(QuotaAction)
        .filter_by(id=action_id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    if run.operation_id == token:
        run.last_error = error
        run.state = "failed" if error else "issued"
        run.details = {**run.details, "stop_finished_at": _now().isoformat()}
    db.commit()


def _deliver_warnings(db):
    ids = [
        identity
        for (identity,) in db.query(QuotaAction.id).filter(
            QuotaAction.kind == "warning",
            QuotaAction.state.in_(("pending", "issued", "failed")),
        )
    ]
    db.commit()
    for identity in ids:
        with _execution_guard(db, f"warning:{identity}") as acquired:
            if acquired:
                _deliver_warning(db, identity)


def _deliver_warning(db, identity):
    from app.services.ws_pubsub import notify_project

    warning = (
        db.query(QuotaAction)
        .filter_by(id=identity)
        .with_for_update()
        .populate_existing()
        .one()
    )
    now = _now()
    if warning.state == "completed" or (
        warning.next_attempt_at and accounting.utc(warning.next_attempt_at) > now
    ):
        db.commit()
        return
    recipients = [
        project.id
        for project in accounting._projects(db, warning.scope, warning.scope_id)
    ]
    message = {
        "type": "quota_warning",
        "event_id": warning.id,
        "scope": warning.scope,
        "scope_id": warning.scope_id,
        "threshold": warning.threshold,
        **warning.details,
    }
    warning.state, warning.attempts = "issued", warning.attempts + 1
    warning.next_attempt_at = now + timedelta(seconds=_backoff(warning.attempts))
    db.commit()
    error = None
    try:
        for project_id in recipients:
            notify_project(project_id, message)
    except Exception:
        logger.exception("Quota warning delivery failed")
        error = "Warning delivery failed; retry pending"
    warning = db.get(QuotaAction, identity)
    warning.state, warning.last_error = (
        ("failed", error) if error else ("completed", None)
    )
    db.commit()


def run_once():
    if not accounting.quotas_enabled():
        return
    from app.core.database import SessionLocal
    from app.services import quota_storage

    with SessionLocal() as inventory:
        project_ids = [
            value
            for (value,) in inventory.query(Project.id).filter(
                Project.host_id.isnot(None)
            )
        ]
    for project_id in project_ids:
        try:
            quota_storage.reconcile_project(project_id)
        except Exception:
            logger.warning(
                "Physical storage observation unavailable for %s; growth remains fenced by freshness",
                project_id,
            )
    with SessionLocal() as db:
        evaluate(db, _now())
        db.commit()
        ids = [
            identity
            for (identity,) in db.query(QuotaAction.id).filter(
                QuotaAction.kind == "stop_run",
                QuotaAction.state.in_(("pending", "issued", "failed")),
            )
        ]
        db.commit()
        ready = []
        for identity in ids:
            try:
                if fence_action(db, identity, _now()):
                    ready.append(identity)
            except Exception:
                db.rollback()
                logger.exception("Quota growth fence failed; durable intent retained")
        # Commit every short control fence before any notifier/queue delivery.
        _deliver_warnings(db)
        for identity in ready:
            try:
                _schedule_action(db, identity, _now())
            except Exception:
                db.rollback()
                logger.exception(
                    "Quota action delivery failed; durable intent retained"
                )


def fence_action(db, action_id, now):
    """Short synchronous control phase; queue availability cannot delay growth cancellation.

    Owns the transaction. Discover operation IDs first, acquire sorted operation
    locks before identities, recheck current causes, and commit without provider I/O.
    The delayed physical stop worker independently rechecks allowance again."""
    run = db.get(QuotaAction, action_id)
    if not run or run.state in {"completed", "cancelled"}:
        db.commit()
        return False
    rows = _lock_action_project(db, run.project_id)
    project = db.get(Project, run.project_id)
    if not project:
        db.commit()
        return False
    current, _unavailable = _evaluate_project(db, project, now, {})
    if current:
        _fence(rows)
    else:
        run.state = "cancelled"
    db.commit()
    return bool(current)


def _schedule_action(db, identity, now):
    """Persist a bounded delivery lease before touching Redis or thread fallback."""
    from app.core.redis import enqueue_job

    run = (
        db.query(QuotaAction)
        .filter_by(id=identity)
        .with_for_update()
        .populate_existing()
        .one()
    )
    delivery = run.details.get("delivery_next_at")
    not_due = run.next_attempt_at and accounting.utc(run.next_attempt_at) > now
    queued = delivery and accounting.utc(datetime.fromisoformat(delivery)) > now
    if run.state in {"completed", "cancelled"} or not_due or queued:
        db.commit()
        return
    run.details = {
        **run.details,
        "delivery_next_at": (now + timedelta(seconds=60)).isoformat(),
    }
    db.commit()
    # Deliberately omit project_id: quota work must not replace a deployment's
    # RQ tracking reference, especially while cancelling provisioning growth.
    enqueue_job(reconcile_worker, identity)


def reconcile_worker(action_id):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        try:
            reconcile_action(db, action_id, _now())
        except Exception:
            db.rollback()
            logger.exception(
                "Quota action reconciliation failed; durable intent retained"
            )


def start_enforcement_poll():
    """Startup pass plus periodic durable-action recovery, including without Redis."""
    global _poll_started
    if not accounting.quotas_enabled() or _poll_started:
        return
    _poll_started = True

    def loop():
        interval = threading.Event()
        while True:
            try:
                run_once()
            except Exception:
                logger.exception("Quota enforcement pass failed")
            interval.wait(30)

    thread = threading.Thread(target=loop, daemon=True, name="quota-enforcement")
    thread.start()
    return thread
