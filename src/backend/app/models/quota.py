"""Durable quota ledger. Identity snapshots deliberately outlive live objects.

Services own transactions, locking, state transitions and attribution. JSON
resource vectors use schemas.quota.Resources; explicit policy limits have typed
SQL columns. No ledger row cascades from project/user/organization deletion.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from pydantic import TypeAdapter
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.core.database import Base
from app.schemas.quota import Money, Resources

_money = TypeAdapter(Money)
_MAX_DECIMAL = "99999999999999999999999999.999999999999"


def _id():
    return str(uuid.uuid4())


class LedgerIdentity:
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_id)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class QuotaPolicy(LedgerIdentity, Base):
    __tablename__ = "quota_policies"
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", name="uq_quota_policy_scope"),
        CheckConstraint(
            "scope IN ('organization', 'user', 'project')", name="ck_quota_policy_scope"
        ),
        CheckConstraint("revision >= 1", name="ck_quota_policy_revision"),
        *(
            CheckConstraint(
                f"{name} IS NULL OR {name} >= 0", name=f"ck_quota_policy_{name}"
            )
            for name in (
                "cpu",
                "ram_bytes",
                "storage_bytes",
                "projects",
                "spending_usd",
            )
        ),
        CheckConstraint(
            f"spending_usd IS NULL OR spending_usd <= {_MAX_DECIMAL}",
            name="ck_quota_policy_finite_spending",
        ),
    )

    scope: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    cpu: Mapped[int | None] = mapped_column(BigInteger)
    ram_bytes: Mapped[int | None] = mapped_column(BigInteger)
    storage_bytes: Mapped[int | None] = mapped_column(BigInteger)
    projects: Mapped[int | None] = mapped_column(BigInteger)
    spending_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @validates("spending_usd")
    def validate_money(self, key, value):
        return None if value is None else _money.validate_python(value)


class OperationIdentity(LedgerIdentity):
    operation_id: Mapped[str] = mapped_column(String(255), unique=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    owner_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    organization_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    requested: Mapped[dict] = mapped_column(JSONB, default=dict)
    observed: Mapped[dict] = mapped_column(JSONB, default=dict)
    observation_id: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @validates("requested", "observed")
    def validate_vector(self, key, value):
        return Resources.model_validate(value).model_dump()


class QuotaReservation(OperationIdentity, Base):
    __tablename__ = "quota_reservations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'dispatched', 'confirmed', 'released', 'failed', 'reconciling')",
            name="ck_quota_reservation_state",
        ),
    )
    state: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    dispatched_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Assign a new dict when updating: operation kind, desired digest/resource
    # identities, queue tracking and retry evidence survive API/worker restarts.
    dispatch_metadata: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )


class QuotaAllocation(OperationIdentity, Base):
    __tablename__ = "quota_allocations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'stopped', 'released', 'reconciling')",
            name="ck_quota_allocation_state",
        ),
    )
    state: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active"
    )
    observed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class QuotaCharge(LedgerIdentity, Base):
    __tablename__ = "quota_charges"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_id", "start_at", "end_at", name="uq_quota_charge_segment"
        ),
        CheckConstraint("end_at > start_at", name="ck_quota_charge_interval"),
        *(
            CheckConstraint(f"{name} >= 0", name=f"ck_quota_charge_{name}")
            for name in ("quantity", "rate", "amount")
        ),
        *(
            CheckConstraint(
                f"{name} <= {_MAX_DECIMAL}", name=f"ck_quota_charge_finite_{name}"
            )
            for name in ("quantity", "rate", "amount")
        ),
    )
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    project_name: Mapped[str] = mapped_column(String(255))
    owner_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    owner_name: Mapped[str] = mapped_column(String(255), default="")
    organization_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    organization_name: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(255))
    start_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    end_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(40))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    rate: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)

    @validates("quantity", "rate", "amount")
    def validate_money(self, key, value):
        return _money.validate_python(value)


class QuotaAudit(LedgerIdentity, Base):
    __tablename__ = "quota_audits"
    actor_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    scope: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    action: Mapped[str] = mapped_column(String(60))
    before: Mapped[dict] = mapped_column(JSONB)
    after: Mapped[dict] = mapped_column(JSONB)
    effective_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class QuotaAction(LedgerIdentity, Base):
    __tablename__ = "quota_actions"
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "scope_id",
            "policy_revision",
            "period_key",
            "kind",
            "threshold",
            "project_id",
            name="uq_quota_action_identity",
        ),
        CheckConstraint(
            "state IN ('pending', 'issued', 'completed', 'failed', 'cancelled')",
            name="ck_quota_action_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_quota_action_attempts"),
    )
    scope: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    policy_revision: Mapped[int] = mapped_column(BigInteger)
    period_key: Mapped[str] = mapped_column(String(40))
    # Empty string is a non-null sentinel for scope-level warnings, preserving uniqueness.
    project_id: Mapped[str] = mapped_column(String(36), default="", server_default="")
    kind: Mapped[str] = mapped_column(String(20))
    threshold: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    operation_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")


class QuotaReadiness(Base):
    __tablename__ = "quota_readiness"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ready: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    checked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    baseline_version: Mapped[str | None] = mapped_column(String(100))
