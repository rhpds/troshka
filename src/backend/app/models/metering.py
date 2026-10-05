from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MeteringRateDefault(Base):
    __tablename__ = "metering_rate_defaults"

    provider_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    rates: Mapped[dict] = mapped_column(JSONB, nullable=False)


class MeteringInterval(Base):
    __tablename__ = "metering_intervals"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(100), nullable=False)
    qty: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    unit_rate: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    host_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProjectInvoice(Base):
    __tablename__ = "project_invoices"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    total_usd: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    line_items: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    period_start: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(8), default="USD", server_default="USD"
    )
    finalized_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MonthlyStatement(Base):
    """Frozen per-owner calendar-month rollup (hybrid: compute then persist)."""

    __tablename__ = "monthly_statements"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "period_start", name="uq_monthly_statements_owner_period"
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    period_start: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    total_usd: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    line_items: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    currency: Mapped[str] = mapped_column(
        String(8), default="USD", server_default="USD"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft"
    )
    finalized_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
