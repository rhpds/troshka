"""Cleanup state and immutable evidence survive Project deletion."""

from sqlalchemy import String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.native_deployment_origin import _immutable


class NativeProjectNamespaceCleanup(Base):
    __tablename__ = "native_project_namespace_cleanups"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeProjectNamespaceEvidence(Base):
    __tablename__ = "native_project_namespace_evidence"
    __table_args__ = (UniqueConstraint("cleanup_id", "phase"),)
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    cleanup_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    phase: Mapped[str] = mapped_column(String(100))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


event.listen(NativeProjectNamespaceEvidence, "before_update", _immutable)
event.listen(NativeProjectNamespaceEvidence, "before_delete", _immutable)
