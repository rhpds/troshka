import datetime
import uuid

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class StoragePool(Base):
    __tablename__ = "storage_pools"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    mode: Mapped[str] = mapped_column(String(20))
    az: Mapped[str | None] = mapped_column(String(50))
    subnet_id: Mapped[str | None] = mapped_column(String(50))

    fsx_filesystem_id: Mapped[str | None] = mapped_column(String(50))
    fsx_dns_name: Mapped[str | None] = mapped_column(String(255))
    fsx_mount_ip: Mapped[str | None] = mapped_column(String(45))
    fsx_throughput_mbps: Mapped[int | None] = mapped_column(Integer)
    fsx_storage_gb: Mapped[int | None] = mapped_column(Integer)

    nfs_endpoint: Mapped[str | None] = mapped_column(String(500))

    ca_cert: Mapped[str | None] = mapped_column(Text)
    ca_key: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default="creating")
    auto_extend_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_extend_threshold_pct: Mapped[int] = mapped_column(Integer, default=80)
    auto_extend_increment_gb: Mapped[int] = mapped_column(Integer, default=64)
    auto_extend_max_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    provider: Mapped["Provider"] = relationship()
    hosts: Mapped[list["Host"]] = relationship(back_populates="storage_pool")
    cache_entries: Mapped[list["SharedCacheEntry"]] = relationship(
        back_populates="storage_pool", cascade="all, delete-orphan"
    )


class SharedCacheEntry(Base):
    __tablename__ = "shared_cache_entries"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    storage_pool_id: Mapped[str] = mapped_column(ForeignKey("storage_pools.id"))
    item_type: Mapped[str] = mapped_column(String(20))
    item_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="downloading")
    file_path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    downloaded_by_host_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    storage_pool: Mapped["StoragePool"] = relationship(back_populates="cache_entries")
