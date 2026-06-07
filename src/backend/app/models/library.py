import datetime
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[str] = mapped_column(String(10))
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    quota_bytes: Mapped[int | None] = mapped_column(Integer)
    used_bytes: Mapped[int] = mapped_column(Integer, default=0)

    owner: Mapped["User | None"] = relationship(back_populates="libraries")
    items: Mapped[list["LibraryItem"]] = relationship(back_populates="library", cascade="all, delete-orphan")


class LibraryItem(Base):
    __tablename__ = "library_items"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    library_id: Mapped[str] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000))
    type: Mapped[str] = mapped_column(String(20))
    format: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    s3_key: Mapped[str | None] = mapped_column(String(500))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    os_variant: Mapped[str | None] = mapped_column(String(50))
    state: Mapped[str] = mapped_column(String(20), default="uploading")
    source_vm_id: Mapped[str | None] = mapped_column(String(36))
    vm_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    library: Mapped["Library"] = relationship(back_populates="items")
    item_disks: Mapped[list["LibraryItemDisk"]] = relationship(back_populates="library_item", cascade="all, delete-orphan")


class LibraryShare(Base):
    __tablename__ = "library_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("library_items.id", ondelete="CASCADE"))
    shared_with_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    permission: Mapped[str] = mapped_column(String(10), default="use")


class LibraryItemDisk(Base):
    __tablename__ = "library_item_disks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    library_item_id: Mapped[str] = mapped_column(ForeignKey("library_items.id", ondelete="CASCADE"))
    s3_key: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    virtual_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    boot_order: Mapped[int] = mapped_column(Integer, default=0)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="uploading")

    library_item: Mapped["LibraryItem"] = relationship(back_populates="item_disks")


class ImageCache(Base):
    __tablename__ = "image_caches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("library_items.id", ondelete="CASCADE"))
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"))
    local_path: Mapped[str] = mapped_column(String(500))
    cached_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
