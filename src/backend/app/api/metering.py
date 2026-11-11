"""Metering rates, live spend, and invoices."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.models.metering import MeteringRateDefault, MonthlyStatement, ProjectInvoice
from app.models.project import Project
from app.models.user import User
from app.schemas.quota import AccountingUnavailable
from app.services.group_access import billing_owner_ids, require_billing_owner
from app.services.metering_math import RATE_KEYS
from app.services.metering_service import (
    finalize_month,
    live_spend,
    load_type_rates,
    merge_rate_maps,
    metering_enabled,
    reconcile_project,
)
from app.services.quota_accounting import (
    charge_rows,
    quotas_enabled,
    require_ready,
    summarize,
)

router = APIRouter(prefix="/metering", tags=["metering"])

ACCESS_DENIED = "Access denied"
METERING_DISABLED = "Metering is disabled"

CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_role("admin"))]
DbSession = Annotated[Session, Depends(get_db)]


def _require_enabled() -> None:
    if not metering_enabled():
        raise HTTPException(status_code=404, detail=METERING_DISABLED)


class RatesBody(BaseModel):
    rates: dict[str, dict]


def _clean_rate_block(block: dict) -> dict:
    out = {}
    for key in RATE_KEYS:
        if key in block and block[key] is not None:
            out[key] = float(block[key])
    return out


@router.get("/rates", responses={404: {"description": METERING_DISABLED}})
def get_rates(user: CurrentUser, db: DbSession):
    _require_enabled()
    return merge_rate_maps(load_type_rates(db))


@router.put("/rates", responses={404: {"description": METERING_DISABLED}})
def put_rates(body: RatesBody, user: AdminUser, db: DbSession):
    _require_enabled()
    for ptype, block in body.rates.items():
        cleaned = _clean_rate_block(block or {})
        row = db.get(MeteringRateDefault, ptype)
        if row:
            row.rates = cleaned
        else:
            db.add(MeteringRateDefault(provider_type=ptype, rates=cleaned))
    db.commit()
    return merge_rate_maps(load_type_rates(db))


def _owner_scope(query, model, user: User, db: Session):
    scope = billing_owner_ids(db, user)
    if scope is None:
        return query
    return query.filter(model.owner_id.in_(scope))


def _owner_emails(db: Session, owner_ids) -> dict[str, str]:
    ids = {oid for oid in owner_ids if oid}
    if not ids:
        return {}
    rows = db.query(User).filter(User.id.in_(ids)).all()
    return {u.id: u.email for u in rows}


@router.get("/invoices", responses={404: {"description": METERING_DISABLED}})
def list_invoices(
    user: CurrentUser,
    db: DbSession,
    own_only: bool = False,
):
    _require_enabled()
    q = _owner_scope(db.query(ProjectInvoice), ProjectInvoice, user, db)
    if own_only:
        q = q.filter(ProjectInvoice.owner_id == user.id)
    rows = q.order_by(ProjectInvoice.finalized_at.desc()).all()
    emails = _owner_emails(db, (r.owner_id for r in rows))
    return [
        _invoice_summary(row, emails.get(row.owner_id) if row.owner_id else None, db)
        for row in rows
    ]


@router.get(
    "/invoices/{invoice_id}",
    responses={
        403: {"description": ACCESS_DENIED},
        404: {"description": "Invoice not found or metering is disabled"},
    },
)
def get_invoice(invoice_id: str, user: CurrentUser, db: DbSession):
    _require_enabled()
    row = db.get(ProjectInvoice, invoice_id)
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")
    require_billing_owner(db, user, row.owner_id)
    emails = _owner_emails(db, [row.owner_id])
    return _invoice_detail(row, emails.get(row.owner_id) if row.owner_id else None, db)


def _invoice_summary(
    row: ProjectInvoice, owner_email: str | None = None, db: Session | None = None
) -> dict:
    data = {
        "id": row.id,
        "project_id": row.project_id,
        "project_name": row.project_name,
        "owner_id": row.owner_id,
        "owner_email": owner_email,
        "total_usd": float(row.total_usd),
        "currency": row.currency,
        "period_start": row.period_start.isoformat() if row.period_start else None,
        "period_end": row.period_end.isoformat() if row.period_end else None,
        "finalized_at": row.finalized_at.isoformat() if row.finalized_at else None,
    }
    if db is not None and quotas_enabled():
        data["total_usd"] = str(
            _ledger_summary(
                db, "project", row.project_id, row.period_start, row.period_end
            )["total"]
        )
        data["accounting_status"] = "ready"
    return data


def _invoice_detail(
    row: ProjectInvoice, owner_email: str | None = None, db: Session | None = None
) -> dict:
    data = _invoice_summary(row, owner_email, db)
    data["line_items"] = row.line_items or {}
    if db is not None and quotas_enabled():
        data["line_items"] = _ledger_summary(
            db, "project", row.project_id, row.period_start, row.period_end
        )["line_items"]
    return data


@router.get("/statements", responses={404: {"description": METERING_DISABLED}})
def list_statements(
    user: CurrentUser,
    db: DbSession,
    own_only: bool = False,
):
    _require_enabled()
    q = _owner_scope(
        db.query(MonthlyStatement).filter(MonthlyStatement.status == "final"),
        MonthlyStatement,
        user,
        db,
    )
    if own_only:
        q = q.filter(MonthlyStatement.owner_id == user.id)
    rows = q.order_by(MonthlyStatement.period_start.desc()).all()
    emails = _owner_emails(db, (r.owner_id for r in rows))
    return [
        _statement_summary(row, emails.get(row.owner_id) if row.owner_id else None, db)
        for row in rows
    ]


@router.get(
    "/statements/{statement_id}",
    responses={
        403: {"description": ACCESS_DENIED},
        404: {"description": "Statement not found or metering is disabled"},
    },
)
def get_statement(statement_id: str, user: CurrentUser, db: DbSession):
    _require_enabled()
    row = db.get(MonthlyStatement, statement_id)
    if not row:
        raise HTTPException(status_code=404, detail="Statement not found")
    require_billing_owner(db, user, row.owner_id)
    emails = _owner_emails(db, [row.owner_id])
    return _statement_detail(
        row, emails.get(row.owner_id) if row.owner_id else None, db
    )


@router.post(
    "/statements/finalize", responses={404: {"description": METERING_DISABLED}}
)
def post_finalize_month(
    user: AdminUser,
    db: DbSession,
    year: Annotated[int, Query()],
    month: Annotated[int, Query(ge=1, le=12)],
):
    """Admin helper to freeze a calendar month (UTC)."""
    _require_enabled()
    rows = finalize_month(db, year, month)
    db.commit()
    return [_statement_summary(row, db=db) for row in rows]


def _statement_summary(
    row: MonthlyStatement, owner_email: str | None = None, db: Session | None = None
) -> dict:
    data = {
        "id": row.id,
        "owner_id": row.owner_id,
        "owner_email": owner_email,
        "period_start": row.period_start.isoformat() if row.period_start else None,
        "period_end": row.period_end.isoformat() if row.period_end else None,
        "total_usd": float(row.total_usd),
        "currency": row.currency,
        "status": row.status,
        "finalized_at": row.finalized_at.isoformat() if row.finalized_at else None,
        "project_count": len((row.line_items or {}).get("by_project") or []),
    }
    if db is not None and quotas_enabled():
        data["total_usd"] = str(
            _ledger_summary(db, "user", row.owner_id, row.period_start, row.period_end)[
                "total"
            ]
        )
        data["accounting_status"] = "ready"
    return data


def _statement_detail(
    row: MonthlyStatement, owner_email: str | None = None, db: Session | None = None
) -> dict:
    data = _statement_summary(row, owner_email, db)
    data["line_items"] = row.line_items or {}
    return data


def _ledger_summary(db: Session, scope, scope_id, start, end):
    try:
        require_ready(db)
        return summarize(charge_rows(db, scope, scope_id, start, end), start, end)
    except AccountingUnavailable as exc:
        raise HTTPException(
            503, {"code": "accounting_unavailable", "message": str(exc)}
        ) from exc


@router.get(
    "/projects/{project_id}",
    responses={
        403: {"description": ACCESS_DENIED},
        404: {"description": "Project not found or metering is disabled"},
    },
)
def get_project_metering(project_id: str, user: CurrentUser, db: DbSession):
    """Live spend for a project (also at /projects/{id}/metering)."""
    return _project_metering(project_id, user, db)


def _project_metering(project_id: str, user: User, db: Session) -> dict:
    _require_enabled()
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    require_billing_owner(db, user, project.owner_id)
    # Delegated billing access must never trigger reconciliation's budget-stop
    # side effects. The regular meter poll still refreshes these persisted rows.
    try:
        if project.owner_id == user.id or user.role == "admin":
            if not quotas_enabled() or project.state != "draft":
                reconcile_project(db, project)
                db.commit()
                db.refresh(project)
        result = live_spend(db, project)
        if quotas_enabled():
            db.commit()
        return result
    except AccountingUnavailable as exc:
        raise HTTPException(
            503, {"code": "accounting_unavailable", "message": str(exc)}
        ) from exc
