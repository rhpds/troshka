"""Retained creation facts; no foreign key to a disposable project row."""

from sqlalchemy import Boolean, ForeignKey, String, Text, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NativeDeploymentOriginIntent(Base):
    __tablename__ = "native_deployment_origin_intents"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    attempt_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeDeploymentNamespaceReceipt(Base):
    __tablename__ = "native_deployment_namespace_receipts"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("native_deployment_origin_intents.id"),
        unique=True,
    )
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeDeploymentRootReceipt(Base):
    __tablename__ = "native_deployment_root_receipts"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("native_deployment_origin_intents.id"),
        unique=True,
    )
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeDeploymentOriginActive(Base):
    __tablename__ = "native_deployment_origin_active"
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("native_deployment_origin_intents.id"),
        unique=True,
    )
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    io_phase: Mapped[str | None] = mapped_column(String(40), nullable=True)
    io_token: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    io_job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class NativeDeploymentOriginLaunch(Base):
    """Encrypted original launch inputs, separate from shareable origin facts."""

    __tablename__ = "native_deployment_origin_launches"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("native_deployment_origin_intents.id"),
        primary_key=True,
    )
    encrypted_payload: Mapped[str] = mapped_column(Text)


def _immutable(_mapper, _connection, _target):
    raise ValueError("Native deployment origin facts are immutable")


for _model in (
    NativeDeploymentOriginIntent,
    NativeDeploymentNamespaceReceipt,
    NativeDeploymentRootReceipt,
    NativeDeploymentOriginLaunch,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
