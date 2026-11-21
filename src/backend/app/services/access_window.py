"""Per-user optional access start/end window helpers."""

from __future__ import annotations

import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def user_within_access_window(user, now: datetime.datetime | None = None) -> bool:
    """True when optional access_starts_at / access_ends_at allow access now.

    Missing bounds impose no limit on that side.
    """
    now = _as_utc(now or datetime.datetime.now(datetime.UTC))
    start = getattr(user, "access_starts_at", None)
    end = getattr(user, "access_ends_at", None)
    if start is not None and now < _as_utc(start):
        return False
    if end is not None and now >= _as_utc(end):
        return False
    return True


def access_window_denial_message(user, now: datetime.datetime | None = None) -> str:
    now = _as_utc(now or datetime.datetime.now(datetime.UTC))
    start = getattr(user, "access_starts_at", None)
    end = getattr(user, "access_ends_at", None)
    if start is not None and now < _as_utc(start):
        return "Access has not started yet for this account"
    if end is not None and now >= _as_utc(end):
        return "Access has expired for this account"
    return "Access is outside the allowed date range for this account"


def require_user_access_window(user, *, action: str = "access") -> None:
    if user_within_access_window(user):
        return
    raise HTTPException(
        403,
        f"Cannot {action}: {access_window_denial_message(user)}",
    )


def require_owner_access_window(
    db: Session, project, *, action: str = "provision"
) -> None:
    """Block power actions when the project owner is outside their access window."""
    from app.models.user import User

    owner = db.query(User).filter_by(id=project.owner_id).first()
    if owner is None:
        raise HTTPException(403, f"Cannot {action}: project owner not found")
    require_user_access_window(owner, action=action)


def normalize_access_bounds(
    starts_at: datetime.datetime | None,
    ends_at: datetime.datetime | None,
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    start = _as_utc(starts_at) if starts_at is not None else None
    end = _as_utc(ends_at) if ends_at is not None else None
    if start is not None and end is not None and start >= end:
        raise HTTPException(400, "access_starts_at must be before access_ends_at")
    return start, end
