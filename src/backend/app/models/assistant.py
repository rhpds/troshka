"""Private assistant content. Project identifiers deliberately survive deletion.

Surviving identifiers never confer authorization: policy requires the live project.
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def now():
    return dt.datetime.now(dt.UTC)


class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    owner_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    project_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), nullable=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: now() + dt.timedelta(days=7),
        index=True,
    )


class AssistantGrant(Base):
    __tablename__ = "assistant_grants"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    guest_checks: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "request_id", "role"),)
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    request_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="running")
    grant_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class AssistantEvidence(Base):
    __tablename__ = "assistant_evidence"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    request_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    grant_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class AssistantAuditEvent(Base):
    __tablename__ = "assistant_audit_events"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    actor_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    conversation_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    check: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )
