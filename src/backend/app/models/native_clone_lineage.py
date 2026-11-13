"""Retained lineage journal bound to an existing managed deployment operation."""

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NativeCloneLineage(Base):
    __tablename__ = "native_clone_lineages"
    __table_args__ = (UniqueConstraint("operation_id", "claim_name"),)
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(256))
    claim_name: Mapped[str] = mapped_column(String(256))
    domain_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    intent: Mapped[dict] = mapped_column(JSONB)
    intent_digest: Mapped[str] = mapped_column(String(64))
    creation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proof: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proof_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
