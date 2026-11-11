"""Authorized aggregate projections, independent of billing report filters."""
from fastapi import HTTPException

from app.models.managed_group import ManagedGroup
from app.models.quota import QuotaAction, QuotaPolicy, QuotaReadiness
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, Usage
from app.services import quota_accounting as accounting
from app.services.metering_math import month_bounds
from app.services.quota_admission import usage
from app.services.quota_enforcement import project_status
from app.services.quota_locks import lock_project, lock_rows
from app.services.quota_policy import authorize_policy, can_edit_policy, get_limits


def _lock_scope(db, scope, target):
    """Serialize transfers/policy writes before checking live read permissions.

    Org reads take org -> project locks through accounting; user/project reads
    start with their user identity. No user lock is acquired after an org lock.
    """
    if scope == "project":
        lock_project(db, target.id, require_organization=False)
    elif scope == "user":
        lock_rows(db, user_ids=(target.id,))
        if target.organization_id:
            lock_rows(db, organization_ids=(target.organization_id,))
    else:
        lock_rows(db, organization_ids=(target.id,))


def _cost_center(db, scope, target):
    if scope == "organization":
        return target.cost_center_id
    owner = target if scope == "user" else db.get(User, target.owner_id)
    org = (
        db.get(ManagedGroup, owner.organization_id)
        if owner and owner.organization_id
        else None
    )
    return org.cost_center_id if org else None


def _spending(db, scope, identity, now):
    try:
        # Failed accrual must not persist partial charges from this read request.
        with db.begin_nested():
            return accounting.spend(db, scope, identity, now)
    except (AccountingUnavailable, HTTPException):
        return None


def _actions(db, scope, identity, now):
    if scope == "project":
        status = project_status(db, identity)
        return {
            key: status[key]
            for key in (
                "spending_state",
                "storage_state",
                "action_state",
                "cause_ids",
                "action_reasons",
            )
            if key in status
        }
    period = accounting.utc(now).strftime("%Y-%m")
    policy = db.query(QuotaPolicy).filter_by(scope=scope, scope_id=identity).first()
    rows = (
        db.query(QuotaAction)
        .filter_by(
            scope=scope,
            scope_id=identity,
            kind="stop",
            policy_revision=policy.revision if policy else 0,
        )
        .filter(QuotaAction.period_key.in_((period, "storage")))
        .all()
    )
    counts = {}
    runs = {
        run.project_id: run.state
        for run in db.query(QuotaAction)
        .filter(
            QuotaAction.kind == "stop_run",
            QuotaAction.project_id.in_([row.project_id for row in rows]),
        )
        .all()
    }
    for row in rows:
        state = runs.get(row.project_id, row.state)
        counts[state] = counts.get(state, 0) + 1
    # Aggregate states only: never expose another member's project/action IDs.
    cap = get_limits(db, scope, identity).storage_bytes
    exhausted = cap is not None and usage(db, scope, identity)[0].storage_bytes >= cap
    reasons = sorted(
        {
            row.details.get("reason", "spending_limit")
            for row in rows
            if row.state != "cancelled"
        }
    )
    return {
        "action_counts": counts,
        "action_reasons": reasons,
        "storage_state": "storage_exceeded_shutdown_pending"
        if exhausted
        else "within_allowance",
    }


def read_usage(db, actor, scope, identity, now):
    target = authorize_policy(db, actor, scope, identity)
    _lock_scope(db, scope, target)
    target = authorize_policy(db, actor, scope, identity)
    editable = can_edit_policy(db, actor, scope, target.id)
    limits = get_limits(db, scope, target.id)
    spent = _spending(db, scope, target.id, now)
    used, reserved = usage(db, scope, target.id)
    baseline = db.get(QuotaReadiness, "baseline")
    from app.services.quota_baseline import VERSION

    ready = bool(
        baseline
        and baseline.ready
        and baseline.baseline_version == VERSION
        and (baseline.details or {}).get("accounting_ready")
    )
    enabled = accounting.quotas_enabled()
    policy = db.query(QuotaPolicy).filter_by(scope=scope, scope_id=target.id).first()
    state = "accounting_unavailable" if spent is None else "within_budget"
    if (
        spent is not None
        and limits.spending_usd is not None
        and spent >= limits.spending_usd
    ):
        state = "budget_exceeded_shutdown_pending"
    actions = _actions(db, scope, target.id, now)
    from app.services.quota_storage import usage_details

    storage = usage_details(db, scope, target.id, now)
    state = actions.pop("spending_state", state)
    result = Usage(
        scope=scope,
        scope_id=target.id,
        limits=limits,
        used=used,
        reserved=reserved,
        spent_usd=spent,
        as_of=now if spent is not None else None,
        reset_at=None if scope == "project" else month_bounds(now.year, now.month)[1],
        accounting_status="unavailable" if spent is None else "ready",
        enforcement_enabled=enabled,
        baseline_ready=ready,
        enforcement_status="disabled"
        if not enabled
        else ("active" if ready else "baseline_unready"),
        can_edit=editable,
        cost_center_id=_cost_center(db, scope, target),
        policy_revision=policy.revision if policy else 0,
        spending_state=state,
        **actions,
        **storage,
    )
    return result
