"""Financial reports limited to the requested group's current members."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.metering import (
    CurrentUser,
    DbSession,
    _invoice_summary,
    _require_enabled,
    _statement_summary,
)
from app.models.metering import MonthlyStatement, ProjectInvoice
from app.services.group_access import require_managed_group
from app.services.group_metering import group_report, scoped_members

router = APIRouter(prefix="/metering/groups", tags=["group metering"])


@router.get("/{group_id}/report")
def report(
    group_id: str,
    user: CurrentUser,
    db: DbSession,
    year: Annotated[int, Query(ge=1, le=9998)],
    month: Annotated[int, Query(ge=1, le=12)],
    owner_id: str | None = None,
):
    _require_enabled()
    group = require_managed_group(db, user, group_id)
    return group_report(
        db,
        group,
        scoped_members(db, group_id, owner_id),
        year,
        month,
        owner_id=owner_id,
    )


def _documents(db, user, group_id, owner_id, model, summary):
    _require_enabled()
    require_managed_group(db, user, group_id)
    members = scoped_members(db, group_id, owner_id)
    emails = {m.id: m.email for m in members}
    query = db.query(model).filter(model.owner_id.in_(emails))
    if model is MonthlyStatement:
        query = query.filter(MonthlyStatement.status == "final")
    rows = query.order_by(model.period_end.desc()).all()
    return [summary(row, emails[row.owner_id]) for row in rows]


@router.get("/{group_id}/invoices")
def invoices(
    group_id: str, user: CurrentUser, db: DbSession, owner_id: str | None = None
):
    return _documents(db, user, group_id, owner_id, ProjectInvoice, _invoice_summary)


@router.get("/{group_id}/statements")
def statements(
    group_id: str, user: CurrentUser, db: DbSession, owner_id: str | None = None
):
    return _documents(
        db, user, group_id, owner_id, MonthlyStatement, _statement_summary
    )
