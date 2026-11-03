"""Group-specific reporting assignments, independent of global roles."""

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ManagedGroup(Base):
    __tablename__ = "managed_groups"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ManagedGroupMember(Base):
    __tablename__ = "managed_group_members"

    group_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("managed_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class ManagedGroupManager(Base):
    __tablename__ = "managed_group_managers"

    group_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("managed_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
