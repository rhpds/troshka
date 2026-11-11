"""Read-only, batched current-member usage reports."""

from collections import defaultdict
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.managed_group import ManagedGroup, ManagedGroupMember
from app.models.metering import MeteringInterval, MonthlyStatement
from app.models.project import Project
from app.models.user import User
from app.schemas.quota import AccountingUnavailable
from app.services.metering_math import month_bounds, period_usage
from app.services.quota_accounting import (
    charge_rows,
    quotas_enabled,
    refresh,
    summarize,
)


def scoped_members(
    db: Session, group_id: str, owner_id: str | None = None
) -> list[User]:
    group = db.get(ManagedGroup, group_id)
    query = db.query(User)
    if group is not None and group.is_organization:
        query = query.filter(User.organization_id == group_id)
    else:
        query = query.join(
            ManagedGroupMember, ManagedGroupMember.user_id == User.id
        ).filter(ManagedGroupMember.group_id == group_id)
    if owner_id is not None:
        query = query.filter(User.id == owner_id)
    members = query.order_by(User.email).all()
    if owner_id is not None and not members:
        raise HTTPException(404, "Group member not found")
    return members


def _payload(interval: MeteringInterval) -> dict:
    return {
        "kind": interval.kind,
        "qty": float(interval.qty),
        "unit_rate": float(interval.unit_rate),
        "started_at": interval.started_at,
        "ended_at": interval.ended_at,
    }


def _member_usage(
    member: User, projects: list[Project], intervals: dict, statement, start, end, now
) -> dict:
    data = {
        "owner_id": member.id,
        "owner_email": member.email,
        "display_name": member.display_name,
    }
    if statement is not None and statement.status == "final":
        return {
            **data,
            "status": "final",
            "total_usd": float(statement.total_usd),
            "by_project": (statement.line_items or {}).get("by_project", []),
        }
    lines = {
        p["project_id"]: p
        for p in (
            (statement.line_items or {}).get("by_project", []) if statement else []
        )
    }
    available = statement is not None
    for project in projects:
        payload = intervals.get(project.id, [])
        available = available or bool(payload)
        total, by_kind = period_usage(payload, start, end, now)
        # Surviving projects replace their stored draft by ID, including zero.
        lines.pop(project.id, None)
        if by_kind:
            lines[project.id] = {
                "project_id": project.id,
                "project_name": project.name,
                "subtotal": total,
                "by_kind": by_kind,
            }
    by_project = sorted(
        lines.values(), key=lambda p: (p.get("project_name") or "", p["project_id"])
    )
    return {
        **data,
        "status": "provisional" if available else "unavailable",
        "total_usd": sum(float(p.get("subtotal") or 0) for p in by_project)
        if available
        else None,
        "by_project": by_project,
    }


def group_report(
    db: Session,
    group: ManagedGroup,
    members: list[User],
    year: int,
    month: int,
    now: datetime | None = None,
    owner_id: str | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    if group.is_organization:
        return _organization_report(db, group, members, year, month, now, owner_id)
    if quotas_enabled():
        return _quota_group_report(db, group, members, year, month, now)
    start, end = month_bounds(year, month)
    owner_ids = [m.id for m in members]
    projects = db.query(Project).filter(Project.owner_id.in_(owner_ids)).all()
    project_ids = [p.id for p in projects]
    rows = (
        db.query(MeteringInterval)
        .filter(MeteringInterval.project_id.in_(project_ids))
        .all()
    )
    intervals = defaultdict(list)
    for row in rows:
        intervals[row.project_id].append(_payload(row))
    statements = {
        s.owner_id: s
        for s in db.query(MonthlyStatement)
        .filter(
            MonthlyStatement.owner_id.in_(owner_ids),
            MonthlyStatement.period_start == start,
        )
        .all()
    }
    by_owner = defaultdict(list)
    for project in projects:
        by_owner[project.owner_id].append(project)
    reports = [
        _member_usage(
            m, by_owner[m.id], intervals, statements.get(m.id), start, end, now
        )
        for m in members
    ]
    emails = {m.id: m.email for m in members}
    active = []
    for project in projects:
        if project.state == "draft":
            continue
        total, lines = period_usage(
            intervals[project.id], datetime.min.replace(tzinfo=UTC), now, now
        )
        active.append(
            {
                "id": project.id,
                "name": project.name,
                "state": project.state,
                "owner_id": project.owner_id,
                "owner_email": emails[project.owner_id],
                "total_usd": total if intervals[project.id] else None,
                "line_items": {"by_kind": lines},
            }
        )
    return {
        "group": {"id": group.id, "name": group.name},
        "as_of": now.isoformat(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "currency": "USD",
        "total_usd": sum(r["total_usd"] or 0 for r in reports),
        "members": reports,
        "active_projects": active,
    }


def _quota_member_report(db, member, start, end, now):
    base = {
        "owner_id": member.id,
        "owner_email": member.email,
        "display_name": member.display_name,
    }
    try:
        refresh(db, "user", member.id, now)
        rows = charge_rows(db, "user", member.id, start, end)
        lines = []
        for pid in sorted({r.project_id for r in rows}):
            project_rows = [r for r in rows if r.project_id == pid]
            summary = summarize(project_rows, start, min(end, now))
            lines.append(
                {
                    "project_id": pid,
                    "project_name": project_rows[0].project_name,
                    "subtotal": str(summary["total"]),
                    "by_kind": summary["line_items"]["by_kind"],
                }
            )
        return {
            **base,
            "status": "provisional",
            "accounting_status": "ready",
            "total_usd": str(summarize(rows, start, min(end, now))["total"]),
            "by_project": lines,
        }
    except AccountingUnavailable as exc:
        return {
            **base,
            "status": "unavailable",
            "accounting_status": "unavailable",
            "total_usd": None,
            "by_project": [],
            "reason": str(exc),
        }


def _organization_member(member, rows, start, end, ready):
    lines = []
    for pid in sorted({row.project_id for row in rows}):
        project_rows = [row for row in rows if row.project_id == pid]
        summary = summarize(project_rows, start, end)
        lines.append(
            {
                "project_id": pid,
                "project_name": project_rows[0].project_name,
                "subtotal": str(summary["total"]),
                "by_kind": summary["line_items"]["by_kind"],
            }
        )
    return {
        "owner_id": member.id,
        "owner_email": member.email,
        "display_name": member.display_name,
        "status": "provisional" if ready else "unavailable",
        "accounting_status": "ready" if ready else "unavailable",
        "total_usd": str(summarize(rows, start, end)["total"]) if ready else None,
        "by_project": lines,
    }


def _organization_report(db, group, members, year, month, now, owner_id):
    start, end = month_bounds(year, month)
    ready, reason = True, None
    try:
        refresh(db, "organization", group.id, now)
        history = charge_rows(db, "organization", group.id, end=now)
    except AccountingUnavailable as exc:
        ready, reason, history = False, str(exc), []
    if owner_id is not None:
        history = [row for row in history if row.owner_id == owner_id]
    reports = [
        _organization_member(
            member,
            [row for row in history if row.owner_id == member.id],
            start,
            min(end, now),
            ready,
        )
        for member in members
    ]
    emails = {member.id: member.email for member in members}
    active = []
    for project in (
        db.query(Project)
        .filter(Project.owner_id.in_(emails), Project.state != "draft")
        .all()
    ):
        summary = summarize(
            [row for row in history if row.project_id == project.id], end=now
        )
        active.append(
            {
                "id": project.id,
                "name": project.name,
                "state": project.state,
                "owner_id": project.owner_id,
                "owner_email": emails[project.owner_id],
                "total_usd": str(summary["total"]) if ready else None,
                "line_items": summary["line_items"],
            }
        )
    return {
        "group": {"id": group.id, "name": group.name},
        "as_of": now.isoformat(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "currency": "USD",
        "accounting_status": "ready" if ready else "unavailable",
        "reason": reason,
        "total_usd": str(summarize(history, start, min(end, now))["total"])
        if ready
        else None,
        "members": reports,
        "active_projects": active,
    }


def _quota_group_report(db, group, members, year, month, now):
    from decimal import Decimal

    start, end = month_bounds(year, month)
    reports = [_quota_member_report(db, member, start, end, now) for member in members]
    ready = all(row["accounting_status"] == "ready" for row in reports)
    total = (
        sum((Decimal(row["total_usd"]) for row in reports), Decimal(0))
        if ready
        else None
    )
    emails = {member.id: member.email for member in members}
    active = []
    for project in (
        db.query(Project)
        .filter(Project.owner_id.in_(emails), Project.state != "draft")
        .all()
    ):
        report = next(row for row in reports if row["owner_id"] == project.owner_id)
        rows = (
            charge_rows(db, "project", project.id, end=now)
            if report["accounting_status"] == "ready"
            else []
        )
        summary = summarize(rows, end=now)
        active.append(
            {
                "id": project.id,
                "name": project.name,
                "state": project.state,
                "owner_id": project.owner_id,
                "owner_email": emails[project.owner_id],
                "total_usd": str(summary["total"])
                if report["accounting_status"] == "ready"
                else None,
                "line_items": summary["line_items"],
            }
        )
    return {
        "group": {"id": group.id, "name": group.name},
        "as_of": now.isoformat(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "currency": "USD",
        "accounting_status": "ready" if ready else "unavailable",
        "total_usd": str(total) if total is not None else None,
        "members": reports,
        "active_projects": active,
    }
