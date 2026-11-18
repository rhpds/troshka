"""Explicit legacy custody attestations, independent of deployment history."""

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NativeCustodyRequest(Base):
    __tablename__ = "native_custody_requests"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeCustodyReceipt(Base):
    __tablename__ = "native_custody_receipts"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeCustodyReconciliationRequest(Base):
    __tablename__ = "native_custody_reconciliation_requests"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeCustodySuccessor(Base):
    """Append-only transition; one successor per original or successor receipt."""

    __tablename__ = "native_custody_successors"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    predecessor_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True)
    predecessor_digest: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
