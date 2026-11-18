"""Retained pre-project-I/O authority. Project deletion never cascades here."""

from sqlalchemy import ForeignKey, String, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.native_deployment_origin import _immutable


class NativeDeploymentCleanBaseline(Base):
    __tablename__ = "native_deployment_clean_baselines"
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeDeploymentPreflight(Base):
    __tablename__ = "native_deployment_preflights"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeDeploymentPreflightDecision(Base):
    __tablename__ = "native_deployment_preflight_decisions"
    attempt_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("native_deployment_preflights.id"),
        primary_key=True,
    )
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeDeploymentPreflightActive(Base):
    __tablename__ = "native_deployment_preflight_active"
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("native_deployment_preflights.id"), unique=True
    )


for _model in (
    NativeDeploymentCleanBaseline,
    NativeDeploymentPreflight,
    NativeDeploymentPreflightDecision,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
