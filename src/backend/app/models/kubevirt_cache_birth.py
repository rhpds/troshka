"""Durable source creation journal; retained independently of live objects."""

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NativeCacheBirth(Base):
    __tablename__ = "native_cache_births"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    domain_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    intent_digest: Mapped[str] = mapped_column(String(64))
    intent: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20))
    create_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    receipt: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    receipt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
