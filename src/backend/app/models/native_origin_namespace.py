"""Support and namespace-only history, retained independently of Project rows."""

from sqlalchemy import String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.native_deployment_origin import _immutable


class NativeOriginSupportHandoff(Base):
    __tablename__ = "native_origin_support_handoffs"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginSupportReceipt(Base):
    __tablename__ = "native_origin_support_receipts"
    handoff_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginNamespaceCleanup(Base):
    __tablename__ = "native_origin_namespace_cleanups"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginNamespaceEvidence(Base):
    __tablename__ = "native_origin_namespace_evidence"
    __table_args__ = (UniqueConstraint("cleanup_id", "phase"),)
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    cleanup_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    phase: Mapped[str] = mapped_column(String(40))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


for _model in (
    NativeOriginSupportHandoff,
    NativeOriginSupportReceipt,
    NativeOriginNamespaceEvidence,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
