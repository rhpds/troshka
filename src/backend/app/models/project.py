import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id"))
    host_type: Mapped[str] = mapped_column(String(20), default="shared")
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"))
    state: Mapped[str] = mapped_column(String(20), default="draft")
    public_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    guest_permission: Mapped[str] = mapped_column(String(20), default="console_only")
    run_timer_hours: Mapped[int | None] = mapped_column(Integer)
    run_timer_max_ext_hours: Mapped[int | None] = mapped_column(Integer)
    run_timer_started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    lifetime_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    poweroff_mode: Mapped[str] = mapped_column(String(20), default="simultaneous")
    topology: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=lambda: {"nodes": [], "edges": []}
    )
    vni_map: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    deployed_topology: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    deploy_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    deploy_step: Mapped[str | None] = mapped_column(String(30), nullable=True)
    deploy_progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ocp_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ocp_install_elapsed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    guid: Mapped[str | None] = mapped_column(String(50), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dns_provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("dns_providers.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped["User"] = relationship(back_populates="projects")
    vms: Mapped[list["VM"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    networks: Mapped[list["Network"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    disks: Mapped[list["Disk"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    shares: Mapped[list["ProjectShare"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectShare(Base):
    __tablename__ = "project_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    permission: Mapped[str] = mapped_column(String(20), default="view")

    project: Mapped["Project"] = relationship(back_populates="shares")
