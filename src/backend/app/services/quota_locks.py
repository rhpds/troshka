"""Shared identity locks. Callers commit/rollback; scope changes require rollback.

Admission/transfers: sorted users -> sorted organizations -> sorted projects.
Admission operation advisory locks precede every identity tier.
Background metering: organizations -> projects only, never subsequently users.
The latter may be called under admission locks without acquiring another user.
PostgreSQL READ COMMITTED is required; SQLite is not a concurrency boundary.
"""

from sqlalchemy.orm import Session

from app.models.managed_group import ManagedGroup
from app.models.project import Project
from app.models.user import User
from app.schemas.quota import AccountingUnavailable


class ScopeChanged(AccountingUnavailable):
    """Rollback the entire caller transaction, resolve identities and retry."""


def lock_rows(db: Session, *, user_ids=(), organization_ids=(), project_ids=()) -> None:
    """Acquire each tier in sorted order; do not call with users after org locks."""
    for model, identities in (
        (User, user_ids),
        (ManagedGroup, organization_ids),
        (Project, project_ids),
    ):
        ids = sorted(set(identities))
        if ids:
            rows = (
                db.query(model)
                .filter(model.id.in_(ids))
                .order_by(model.id)
                .with_for_update()
                .populate_existing()
                .all()
            )
            if len(rows) != len(ids):
                raise AccountingUnavailable("Quota identity no longer exists")


def lock_project(
    db: Session,
    project_id: str,
    *,
    membership: bool = True,
    require_organization: bool = True,
) -> tuple[Project, User, ManagedGroup | None]:
    """Lock admission identities, or use the org->project metering path.

    Read scalars before and after locks, bypassing stale ORM identity-map data. A
    membership/owner race aborts rather than acquiring a new organization out of order.
    """
    initial = (
        db.query(Project.owner_id, User.organization_id)
        .join(User, Project.owner_id == User.id)
        .filter(Project.id == project_id)
        .first()
    )
    if not initial or (require_organization and not initial.organization_id):
        raise AccountingUnavailable("Project requires an assigned owner organization")
    owner_id, organization_id = initial
    lock_rows(
        db,
        user_ids=(owner_id,) if membership else (),
        organization_ids=(organization_id,) if organization_id else (),
        project_ids=(project_id,),
    )
    current = (
        db.query(Project.owner_id, User.organization_id)
        .join(User, Project.owner_id == User.id)
        .filter(Project.id == project_id)
        .first()
    )
    if current != initial:
        raise ScopeChanged("Quota identity changed; rollback and retry")
    # Metering intentionally never locks users, but must discard cached membership
    # before attribution. The held organization lock serializes supported transfers.
    owner = db.query(User).filter_by(id=owner_id).populate_existing().one()
    organization = db.get(ManagedGroup, organization_id) if organization_id else None
    if organization is not None and not organization.is_organization:
        raise AccountingUnavailable("Quota parent is not an organization")
    project = db.query(Project).filter_by(id=project_id).one()
    return project, owner, organization


def lock_metering_project(db: Session, project_id: str):
    return lock_project(db, project_id, membership=False, require_organization=False)


def lock_metering_projects(db: Session, project_ids) -> list[Project]:
    """Lock a complete metering batch before any project locks are acquired.

    Ordinary unassigned legacy sources remain billable without inventing a parent.
    Batch callers must not acquire another organization's lock after this helper.
    """
    ids = sorted(set(project_ids))
    query = (
        db.query(Project.id, Project.owner_id, User.organization_id)
        .join(User, Project.owner_id == User.id)
        .filter(Project.id.in_(ids))
        .order_by(Project.id)
    )
    initial = query.all()
    if len(initial) != len(ids):
        raise AccountingUnavailable("Metering batch requires retained owners")
    lock_rows(
        db,
        organization_ids=[
            row.organization_id for row in initial if row.organization_id
        ],
        project_ids=ids,
    )
    if query.all() != initial:
        raise ScopeChanged("Quota identity changed; rollback and retry")
    db.query(User).filter(
        User.id.in_([row.owner_id for row in initial])
    ).populate_existing().all()
    return db.query(Project).filter(Project.id.in_(ids)).order_by(Project.id).all()


def _retained_project_identity(db, project_id):
    from app.models.metering import MeteringInterval
    from app.models.quota import QuotaAllocation, QuotaReservation

    owner = (
        db.query(Project.owner_id, User.organization_id)
        .join(User, Project.owner_id == User.id)
        .filter(Project.id == project_id)
        .first()
    )
    if owner is None:
        raise AccountingUnavailable("Cleanup requires a retained project owner")
    organizations = {owner.organization_id} if owner.organization_id else set()
    for model in (QuotaReservation, QuotaAllocation):
        organizations.update(
            value
            for (value,) in db.query(model.organization_id).filter_by(
                project_id=project_id
            )
        )
    for (identity,) in db.query(MeteringInterval.quota_attribution).filter_by(
        project_id=project_id
    ):
        if identity and identity.get("organization_id"):
            organizations.add(identity["organization_id"])
    return owner.owner_id, owner.organization_id, tuple(sorted(organizations))


def lock_retained_project(db: Session, project_id: str) -> Project:
    """Cleanup locks owner -> all retained organizations -> project, without assignment.

    Callers changing allocations first lock the complete operation set. Retained
    source identity, rather than current membership, owns already incurred costs.
    Deleted organization metadata does not invalidate retained charge identity.
    """
    db.flush()
    initial = _retained_project_identity(db, project_id)
    owner_id, _, organizations = initial
    existing = [
        value
        for (value,) in db.query(ManagedGroup.id).filter(
            ManagedGroup.id.in_(organizations)
        )
    ]
    lock_rows(
        db, user_ids=(owner_id,), organization_ids=existing, project_ids=(project_id,)
    )
    if _retained_project_identity(db, project_id) != initial:
        raise ScopeChanged(
            "Retained accounting identity changed; rollback and retry cleanup"
        )
    return db.query(Project).filter_by(id=project_id).one()
