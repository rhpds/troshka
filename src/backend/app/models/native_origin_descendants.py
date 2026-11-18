"""Immutable dispatch closure and settlement, independent of invocation exit."""

from sqlalchemy import String, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.native_deployment_origin import _immutable


class NativeOriginDescendantHandoff(Base):
    __tablename__ = "native_origin_descendant_handoffs"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    parent_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginDescendantClaim(Base):
    __tablename__ = "native_origin_descendant_claims"
    handoff_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginWriterClosure(Base):
    __tablename__ = "native_origin_writer_closures"
    writer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginHoldResolution(Base):
    __tablename__ = "native_origin_hold_resolutions"
    writer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginDispatchClosure(Base):
    __tablename__ = "native_origin_dispatch_closures"
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginRemoteHold(Base):
    __tablename__ = "native_origin_remote_holds"
    writer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


for _model in (
    NativeOriginDescendantHandoff,
    NativeOriginDescendantClaim,
    NativeOriginWriterClosure,
    NativeOriginHoldResolution,
    NativeOriginDispatchClosure,
    NativeOriginRemoteHold,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
