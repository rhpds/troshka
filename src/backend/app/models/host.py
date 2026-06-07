import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id"))
    instance_id: Mapped[str | None] = mapped_column(String(100))
    instance_type: Mapped[str | None] = mapped_column(String(50))
    region: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="provisioning")
    host_type: Mapped[str] = mapped_column(String(20), default="shared")
    total_vcpus: Mapped[int] = mapped_column(Integer, default=0)
    total_ram_mb: Mapped[int] = mapped_column(Integer, default=0)
    used_vcpus: Mapped[int] = mapped_column(Integer, default=0)
    used_ram_mb: Mapped[int] = mapped_column(Integer, default=0)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    agent_status: Mapped[str] = mapped_column(String(20), default="disconnected")
    key_pair_name: Mapped[str | None] = mapped_column(String(100))
    private_key: Mapped[str | None] = mapped_column(Text)
    storage_size_gb: Mapped[int] = mapped_column(Integer, default=500)
    max_eips: Mapped[int] = mapped_column(Integer, default=0)
    last_health_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    provider: Mapped["Provider | None"] = relationship(back_populates="hosts")
    vms: Mapped[list["VM"]] = relationship(back_populates="host")


class HostAssignment(Base):
    __tablename__ = "host_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"))
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
