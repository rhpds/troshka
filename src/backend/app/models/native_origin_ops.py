"""Immutable support-operation evidence; never cascaded with a project."""

from sqlalchemy import String, Text, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.native_deployment_origin import _immutable


class NativeOriginOpsOperation(Base):
    __tablename__ = "native_origin_ops_operations"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    writer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    encrypted_body: Mapped[str | None] = mapped_column(Text)


class NativeOriginOpsFact(Base):
    __tablename__ = "native_origin_ops_facts"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    operation_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


for _model in (NativeOriginOpsOperation, NativeOriginOpsFact):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
