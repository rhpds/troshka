from __future__ import annotations

import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorkloadRun(Base):
    __tablename__ = "workload_runs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # catalog_item | ad_hoc
    catalog_item: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_fqcn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scm_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ee_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_map: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # AgnosticD-compatible requirements_content (collections/roles with git
    # sources), passed through verbatim to the runner for install before main.yml.
    requirements_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # User-supplied extra_vars (YAML/JSON parsed dict) merged into the final extra_vars
    extra_vars: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Holds the bounded (~256 KB) log tail persisted on finalize (reused to avoid migration)
    log_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    resulting_pattern_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
