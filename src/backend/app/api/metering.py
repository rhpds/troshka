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
from app.services.metering_math import RATE_KEYS
from app.services.metering_service import (
    finalize_month,
    live_spend,
    load_type_rates,
    merge_rate_maps,
    metering_enabled,
    reconcile_project,
)

router = APIRouter(prefix="/metering", tags=["metering"])

CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_role("admin"))]
DbSession = Annotated[Session, Depends(get_db)]


def _require_enabled() -> None:
    if not metering_enabled():
        raise HTTPException(status_code=404, detail="Metering is disabled")


class RatesBody(BaseModel):
    rates: dict[str, dict]


def _clean_rate_block(block: dict) -> dict:
    out = {}
    for key in RATE_KEYS:
        if key in block and block[key] is not None:
            out[key] = float(block[key])
    return out


@router.get("/rates")
def get_rates(user: CurrentUser, db: DbSession):
    _require_enabled()
    return merge_rate_maps(load_type_rates(db))


@router.put("/rates")
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


def _owner_scope(query, model, user: User):
    if user.role == "admin":
        return query
    return query.filter(model.owner_id == user.id)


def _owner_emails(db: Session, owner_ids) -> dict[str, str]:
    ids = {oid for oid in owner_ids if oid}
    if not ids:
        return {}
    rows = db.query(User).filter(User.id.in_(ids)).all()
    return {u.id: u.email for u in rows}


@router.get("/invoices")
def list_invoices(
    user: CurrentUser,
    db: DbSession,
):
    _require_enabled()
    q = _owner_scope(db.query(ProjectInvoice), ProjectInvoice, user)
    rows = q.order_by(ProjectInvoice.finalized_at.desc()).all()
    emails = _owner_emails(db, (r.owner_id for r in rows))
    return [
        _invoice_summary(row, emails.get(row.owner_id) if row.owner_id else None)
        for row in rows
    ]


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, user: CurrentUser, db: DbSession):
    _require_enabled()
    row = db.get(ProjectInvoice, invoice_id)
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if row.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    emails = _owner_emails(db, [row.owner_id])
    return _invoice_detail(row, emails.get(row.owner_id) if row.owner_id else None)


def _invoice_summary(row: ProjectInvoice, owner_email: str | None = None) -> dict:
    return {
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


def _invoice_detail(row: ProjectInvoice, owner_email: str | None = None) -> dict:
    data = _invoice_summary(row, owner_email)
    data["line_items"] = row.line_items or {}
    return data


@router.get("/statements")
def list_statements(
    user: CurrentUser,
    db: DbSession,
):
    _require_enabled()
    q = _owner_scope(
        db.query(MonthlyStatement).filter(MonthlyStatement.status == "final"),
        MonthlyStatement,
        user,
    )
    rows = q.order_by(MonthlyStatement.period_start.desc()).all()
    emails = _owner_emails(db, (r.owner_id for r in rows))
    return [
        _statement_summary(row, emails.get(row.owner_id) if row.owner_id else None)
        for row in rows
    ]


@router.get("/statements/{statement_id}")
def get_statement(statement_id: str, user: CurrentUser, db: DbSession):
    _require_enabled()
    row = db.get(MonthlyStatement, statement_id)
    if not row:
        raise HTTPException(status_code=404, detail="Statement not found")
    if row.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    emails = _owner_emails(db, [row.owner_id])
    return _statement_detail(row, emails.get(row.owner_id) if row.owner_id else None)


@router.post("/statements/finalize")
def post_finalize_month(
    user: AdminUser,
    db: DbSession,
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
):
    """Admin helper to freeze a calendar month (UTC)."""
    _require_enabled()
    rows = finalize_month(db, year, month)
    db.commit()
    return [_statement_summary(row) for row in rows]


def _statement_summary(row: MonthlyStatement, owner_email: str | None = None) -> dict:
    return {
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


def _statement_detail(row: MonthlyStatement, owner_email: str | None = None) -> dict:
    data = _statement_summary(row, owner_email)
    data["line_items"] = row.line_items or {}
    return data


@router.get("/projects/{project_id}")
def get_project_metering(project_id: str, user: CurrentUser, db: DbSession):
    """Live spend for a project (also at /projects/{id}/metering)."""
    return _project_metering(project_id, user, db)


def _project_metering(project_id: str, user: User, db: Session) -> dict:
    _require_enabled()
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    reconcile_project(db, project)
    db.commit()
    db.refresh(project)
    return live_spend(db, project)
