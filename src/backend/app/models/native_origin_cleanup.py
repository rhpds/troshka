"""Retained native cleanup evidence, independent of disposable project rows."""

from sqlalchemy import String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.native_deployment_origin import _immutable


class NativeOriginWriter(Base):
    __tablename__ = "native_origin_writers"
    __table_args__ = (UniqueConstraint("intent_id", "execution_id"),)
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    execution_id: Mapped[str] = mapped_column(String(200))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginWriterTerminal(Base):
    __tablename__ = "native_origin_writer_terminals"
    writer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginWriterHold(Base):
    __tablename__ = "native_origin_writer_holds"
    writer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginRootSubmission(Base):
    __tablename__ = "native_origin_root_submissions"
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


for _model in (
    NativeOriginWriter,
    NativeOriginWriterTerminal,
    NativeOriginWriterHold,
    NativeOriginRootSubmission,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
