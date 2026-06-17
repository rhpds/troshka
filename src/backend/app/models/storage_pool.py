import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class StoragePool(Base):
    __tablename__ = "storage_pools"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
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
    nfs_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ceph_subvolume_group: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # GCP NetApp Volumes
    netapp_pool_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    netapp_mount_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    netapp_volume_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    netapp_service_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    netapp_capacity_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Azure Files NFS
    azure_storage_account: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    azure_file_share_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    azure_file_share_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    azure_files_capacity_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    azure_files_iops: Mapped[int | None] = mapped_column(Integer, nullable=True)
    azure_files_throughput: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ca_cert: Mapped[str | None] = mapped_column(Text)
    ca_key: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default="creating")
    auto_extend_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_extend_threshold_pct: Mapped[int] = mapped_column(Integer, default=80)
    auto_extend_increment_gb: Mapped[int] = mapped_column(Integer, default=64)
    auto_extend_max_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    worker_host_id: Mapped[str | None] = mapped_column(
        ForeignKey("hosts.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    worker_instance_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pb_auto_sleep_minutes: Mapped[int] = mapped_column(Integer, default=30)
    pb_last_activity_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    provider: Mapped["Provider"] = relationship()
    hosts: Mapped[list["Host"]] = relationship(
        back_populates="storage_pool",
        foreign_keys="[Host.storage_pool_id]",
    )
    cache_entries: Mapped[list["SharedCacheEntry"]] = relationship(
        back_populates="storage_pool", cascade="all, delete-orphan"
    )


class SharedCacheEntry(Base):
    __tablename__ = "shared_cache_entries"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
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
