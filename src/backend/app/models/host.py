import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
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
    private_ip: Mapped[str | None] = mapped_column(String(45))
    agent_status: Mapped[str] = mapped_column(String(20), default="disconnected")
    key_pair_name: Mapped[str | None] = mapped_column(String(100))
    private_key: Mapped[str | None] = mapped_column(Text)
    storage_size_gb: Mapped[int] = mapped_column(Integer, default=500)
    max_eips: Mapped[int] = mapped_column(Integer, default=0)
    last_health_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    agent_token: Mapped[str | None] = mapped_column(Text)
    agent_cert_fingerprint: Mapped[str | None] = mapped_column(String(100))
    agent_version: Mapped[str | None] = mapped_column(String(50))
    storage_pool_id: Mapped[str | None] = mapped_column(ForeignKey("storage_pools.id"))
    storage_warnings: Mapped[list | None] = mapped_column(JSONB, default=None)
    auto_extend_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_extend_threshold_pct: Mapped[int] = mapped_column(Integer, default=80)
    auto_extend_increment_gb: Mapped[int] = mapped_column(Integer, default=100)
    auto_extend_max_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    console_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    provider: Mapped["Provider | None"] = relationship(back_populates="hosts")
    storage_pool: Mapped["StoragePool | None"] = relationship(
        back_populates="hosts", foreign_keys="[Host.storage_pool_id]"
    )
    vms: Mapped[list["VM"]] = relationship(back_populates="host")


class HostAssignment(Base):
    __tablename__ = "host_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"))
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
