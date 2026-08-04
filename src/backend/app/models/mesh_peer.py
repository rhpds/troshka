from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProjectMeshPeer(Base):
    __tablename__ = "project_mesh_peers"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("providers.id"), nullable=True
    )
    peer_type: Mapped[str] = mapped_column(String(20), nullable=False)
    wg_public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    wg_private_key: Mapped[str] = mapped_column(String(256), nullable=False)
    wg_endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    wg_address: Mapped[str] = mapped_column(String(32), nullable=False)
    wg_port: Mapped[int] = mapped_column(Integer, nullable=False)
    is_network_host: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
