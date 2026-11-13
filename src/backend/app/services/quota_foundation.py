"""Missing quota seed initialization for explicit database preparation only.

create_all plus Alembic stamp skips migration data inserts. Repair their three
foundation rows without replaying the migration's historical user attribution.
This never initializes accounting, certifies readiness, or enables enforcement.
"""

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.models.managed_group import ManagedGroup
from app.models.quota import QuotaPolicy, QuotaReadiness

# Reserved identities from f62c48d91a07; display names/descriptions are mutable.
DEFAULT_ORGANIZATION_ID = "dc591874-9d97-4bf5-ab31-581bb95cc645"
_GROUP_KEY = "quota-default-" + DEFAULT_ORGANIZATION_ID
_POLICY_ID = "cc4ecf45-0811-48a5-bca2-e440a3fbc55c"


def _group(db):
    rows = (
        db.query(ManagedGroup)
        .filter(
            or_(
                ManagedGroup.id == DEFAULT_ORGANIZATION_ID,
                ManagedGroup.normalized_name == _GROUP_KEY,
            )
        )
        .all()
    )
    if any(
        row.id != DEFAULT_ORGANIZATION_ID
        or row.normalized_name != _GROUP_KEY
        or not row.is_organization
        for row in rows
    ):
        raise RuntimeError(
            "Conflicting quota foundation group identity; review required"
        )
    if not rows:
        db.add(
            ManagedGroup(
                id=DEFAULT_ORGANIZATION_ID,
                name="Default organization",
                normalized_name=_GROUP_KEY,
                description="Migration attribution; not evidence of historical employer",
                is_organization=True,
            )
        )


def _policy(db):
    rows = (
        db.query(QuotaPolicy)
        .filter(
            or_(
                QuotaPolicy.id == _POLICY_ID,
                (QuotaPolicy.scope == "organization")
                & (QuotaPolicy.scope_id == DEFAULT_ORGANIZATION_ID),
            )
        )
        .all()
    )
    if any(
        row.id != _POLICY_ID
        or row.scope != "organization"
        or row.scope_id != DEFAULT_ORGANIZATION_ID
        for row in rows
    ):
        raise RuntimeError(
            "Conflicting quota foundation policy identity; review required"
        )
    if not rows:
        db.add(
            QuotaPolicy(
                id=_POLICY_ID, scope="organization", scope_id=DEFAULT_ORGANIZATION_ID
            )
        )


def initialize(engine):
    """Insert only absent seeds, atomically; existing policy/readiness is retained."""
    with Session(engine) as db, db.begin():
        if engine.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(724827)"))
        _group(db)
        _policy(db)
        if db.get(QuotaReadiness, "baseline") is None:
            db.add(QuotaReadiness(id="baseline", ready=False, details={}))
