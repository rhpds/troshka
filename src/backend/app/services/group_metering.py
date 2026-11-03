"""Read-only, batched current-member usage reports."""

from collections import defaultdict
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.managed_group import ManagedGroup, ManagedGroupMember
from app.models.metering import MeteringInterval, MonthlyStatement
from app.models.project import Project
from app.models.user import User
from app.services.metering_math import month_bounds, period_usage


def scoped_members(
    db: Session, group_id: str, owner_id: str | None = None
) -> list[User]:
    query = (
        db.query(User)
        .join(ManagedGroupMember, ManagedGroupMember.user_id == User.id)
        .filter(ManagedGroupMember.group_id == group_id)
    )
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
) -> dict:
    now = now or datetime.now(UTC)
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
