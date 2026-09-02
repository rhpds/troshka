from __future__ import annotations

import datetime
import hashlib
import secrets
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


def generate_api_key() -> str:
    return f"trk_{secrets.token_urlsafe(32)}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When set, the key is scoped to a single project and limited to `scopes`.
    # NULL project_id = a full-access user key (pre-existing behavior).
    project_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    scopes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    user: Mapped[User] = relationship()

    @property
    def is_scoped(self) -> bool:
        """True when the key is limited to a single project."""
        return self.project_id is not None

    def has_scope(self, perm: str) -> bool:
        """Whether this key grants the given permission.

        Unscoped (full-access) keys grant everything. Scoped keys grant only
        the permissions listed in `scopes`.
        """
        if not self.is_scoped:
            return True
        return perm in (self.scopes or [])
