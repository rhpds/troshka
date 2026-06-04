import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Network(Base):
    __tablename__ = "networks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    cidr: Mapped[str] = mapped_column(String(18))
    dhcp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    dns_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    dns_domain: Mapped[str | None] = mapped_column(String(255))
    dns_upstream: Mapped[bool] = mapped_column(Boolean, default=False)
    pxe_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pxe_profile_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="networks")
    security_rules: Mapped[list["SecurityRule"]] = relationship(
        back_populates="network", cascade="all, delete-orphan"
    )


class SecurityRule(Base):
    __tablename__ = "security_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    network_id: Mapped[str] = mapped_column(ForeignKey("networks.id", ondelete="CASCADE"))
    direction: Mapped[str] = mapped_column(String(10))
    protocol: Mapped[str] = mapped_column(String(10), default="all")
    port_range_start: Mapped[int | None] = mapped_column(Integer)
    port_range_end: Mapped[int | None] = mapped_column(Integer)
    source_cidr: Mapped[str | None] = mapped_column(String(18))
    action: Mapped[str] = mapped_column(String(10), default="allow")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    description: Mapped[str | None] = mapped_column(String(500))

    network: Mapped["Network"] = relationship(back_populates="security_rules")
