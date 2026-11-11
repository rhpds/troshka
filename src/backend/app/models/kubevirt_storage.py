"""Native reporting metadata, deliberately independent of quota and live FKs.

Enrollment, roots, prepared requests and observations are append-only. Only the
collection-domain current-attempt pointer changes. Deleted live identities never
cascade into evidence; historic owner snapshots never grant read access.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NativeReportingTarget(Base):
    __tablename__ = "native_reporting_targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True)


class NativeEnrollmentRequest(Base):
    __tablename__ = "native_enrollment_requests"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeEnrollment(Base):
    __tablename__ = "native_enrollments"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    domain_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeCollectionDomain(Base):
    __tablename__ = "native_collection_domains"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    enrollment_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    latest_report_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    latest_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conflicted: Mapped[bool] = mapped_column(Boolean, default=False)


class NativePoolClaim(Base):
    __tablename__ = "native_pool_claims"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    domain_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeProjectRoot(Base):
    __tablename__ = "native_project_roots"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    domain_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    provider_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    enrollment_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeRetainedBinding(Base):
    __tablename__ = "native_retained_bindings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    root_key: Mapped[str] = mapped_column(String(64), index=True)
    domain_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeStorageSnapshot(Base):
    __tablename__ = "native_storage_snapshots"
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    domain_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    enrollment_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    digest: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted: Mapped[bool] = mapped_column(Boolean)
    reason_codes: Mapped[list] = mapped_column(JSONB)
    payload: Mapped[dict] = mapped_column(JSONB)
