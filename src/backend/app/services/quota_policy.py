"""Authorized quota policies. Callers own transactions; no allocation bypasses.

Policy writers lock live identities in user -> organization -> project order,
also used by admission/transfers. Organization-only edits never lock users.
"""

from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.managed_group import ManagedGroup, ManagedGroupManager
from app.models.project import Project, ProjectShare
from app.models.quota import QuotaAudit, QuotaPolicy, QuotaReadiness
from app.models.user import User
from app.schemas.quota import Limits, Scope
from app.services.quota_locks import lock_project, lock_rows


def _identity(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError):
        raise HTTPException(404, "Quota scope not found") from None


def _row(db: Session, model, identity: str, lock: bool):
    query = db.query(model).filter_by(id=identity)
    if lock:
        tiers = {
            User: "user_ids",
            ManagedGroup: "organization_ids",
            Project: "project_ids",
        }
        lock_rows(db, **{tiers[model]: (identity,)})
        query = query.populate_existing()
    row = query.first()
    if row is None:
        raise HTTPException(404, "Quota scope not found")
    return row


def _organization(db: Session, identity: str, lock: bool) -> ManagedGroup:
    group = _row(db, ManagedGroup, identity, lock)
    if not group.is_organization:
        raise HTTPException(404, "Organization not found")
    return group


def _manages(db: Session, actor: User, group_id: str | None) -> bool:
    return (
        group_id is not None
        and db.query(ManagedGroupManager)
        .filter_by(group_id=group_id, user_id=actor.id)
        .first()
        is not None
    )


def _organization_access(
    db: Session, actor: User, identity: str, write: bool, lock: bool
):
    target = _organization(db, identity, write and lock)
    allowed = not write and (
        _manages(db, actor, target.id)
        or db.query(User).filter_by(id=actor.id, organization_id=target.id).first()
        is not None
    )
    return target, allowed


def _user_access(db: Session, actor: User, identity: str, write: bool, lock: bool):
    target = _row(db, User, identity, write and lock)
    org = (
        _organization(db, target.organization_id, write and lock)
        if target.organization_id
        else None
    )
    allowed = (not write and actor.id == target.id) or (
        org is not None and _manages(db, actor, org.id)
    )
    return target, allowed


def authorize_policy(
    db: Session,
    actor: User,
    scope: Scope,
    scope_id: str,
    *,
    write: bool = False,
    lock: bool = True,
):
    """Resolve and authorize live identities before exposing any policy data."""
    identity = _identity(scope_id)
    if scope == "organization":
        target, allowed = _organization_access(db, actor, identity, write, lock)
    elif scope == "user":
        target, allowed = _user_access(db, actor, identity, write, lock)
    elif scope == "project":
        target = _project_scope(db, identity, write and lock)
        allowed = actor.id == target.owner_id or (
            not write
            and db.query(ProjectShare)
            .filter_by(project_id=target.id, user_id=actor.id)
            .first()
            is not None
        )
    else:
        raise HTTPException(404, "Quota scope not found")
    if actor.role != "admin" and not allowed:
        raise HTTPException(403, "Quota access denied")
    return target


def _project_scope(db: Session, identity: str, lock: bool) -> Project:
    if lock:
        return lock_project(db, identity, require_organization=False)[0]
    return _row(db, Project, identity, False)


def can_edit_policy(db: Session, actor: User, scope: Scope, scope_id: str) -> bool:
    """Permission probe only; never take write locks while reading accounting."""
    try:
        authorize_policy(db, actor, scope, scope_id, write=True, lock=False)
        return True
    except HTTPException as error:
        if error.status_code in (403, 404):
            return False
        raise


def get_limits(db: Session, scope: Scope, scope_id: str) -> Limits:
    """Internal policy read; HTTP callers must authorize_policy first."""
    # Refresh retained policies after identity locks, including when autoflush is
    # disabled. Flush first so the caller's legitimate pending edits survive.
    db.flush()
    policy = (
        db.query(QuotaPolicy)
        .filter_by(scope=scope, scope_id=scope_id)
        .populate_existing()
        .first()
    )
    values = (
        {name: getattr(policy, name) for name in Limits.model_fields} if policy else {}
    )
    if scope == "project":
        # The existing guardrail remains canonical throughout migration/rollout.
        project = _row(db, Project, scope_id, False)
        values["spending_usd"] = project.budget_usd
    return Limits(**values)


def _project_budget(project: Project, limits: Limits) -> None:
    from app.services.quota_accounting import quotas_enabled

    if "spending_usd" not in limits.model_fields_set:
        return
    amount = limits.spending_usd
    if amount is not None and (
        amount > Decimal("999999999999.999999")
        or amount != amount.quantize(Decimal("0.000001"))
    ):
        raise HTTPException(
            422,
            "Project spending limit requires at most 12 integer and 6 fractional digits",
        )
    project.budget_usd = amount
    project.budget_warned = False
    # Explicit legacy cap updates rearm enforcement; quota mode reconciles its
    # durable actions instead. Ordinary restart does not reset this latch.
    if amount is None or not quotas_enabled():
        project.budget_stopped = False


def request_reevaluation(
    db: Session, scope: Scope, scope_id: str, revision: int, now: datetime
) -> None:
    """Durable handoff to the evaluator without invalidating the baseline."""
    # The readiness key is varchar(40); stable UUID keeps one request per scope.
    identity = str(uuid5(NAMESPACE_URL, f"troshka:reevaluate:{scope}:{scope_id}"))
    marker = db.get(QuotaReadiness, identity)
    if marker is None:
        marker = QuotaReadiness(id=identity)
        db.add(marker)
    marker.ready = False
    marker.checked_at = now
    marker.details = {
        "reason": "spending_policy_changed",
        "scope": scope,
        "scope_id": scope_id,
        "policy_revision": revision,
    }


def set_limits(
    db: Session, actor: User, scope: Scope, scope_id: str, limits: Limits, now: datetime
) -> None:
    target = authorize_policy(db, actor, scope, scope_id, write=True)
    scope_id = target.id
    before = get_limits(db, scope, scope_id)
    if scope == "project":
        assert isinstance(target, Project)
        _project_budget(target, limits)
    policy = db.query(QuotaPolicy).filter_by(scope=scope, scope_id=scope_id).first()
    if policy is None:
        policy = QuotaPolicy(scope=scope, scope_id=scope_id, revision=1)
        db.add(policy)
    else:
        policy.revision += 1
    for name in limits.model_fields_set:
        setattr(policy, name, getattr(limits, name))
    # Keep the policy mirror consistent with the canonical legacy guardrail.
    if scope == "project" and isinstance(target, Project):
        policy.spending_usd = target.budget_usd
    policy.updated_at = now
    after = get_limits(db, scope, scope_id)
    db.add(
        QuotaAudit(
            actor_id=actor.id,
            scope=scope,
            scope_id=scope_id,
            action="policy_updated",
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json"),
            effective_at=now,
        )
    )
    if before.spending_usd != after.spending_usd:
        request_reevaluation(db, scope, scope_id, policy.revision, now)
    db.flush()
