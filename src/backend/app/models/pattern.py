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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Pattern(Base):
    __tablename__ = "patterns"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    source_project_id: Mapped[str | None] = mapped_column(String(36))
    topology: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="creating")
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    clock_target: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recert: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    owner: Mapped["User"] = relationship()
    disks: Mapped[list["PatternDisk"]] = relationship(
        back_populates="pattern", cascade="all, delete-orphan"
    )
    shares: Mapped[list["PatternShare"]] = relationship(
        back_populates="pattern", cascade="all, delete-orphan"
    )


class PatternDisk(Base):
    __tablename__ = "pattern_disks"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    pattern_id: Mapped[str] = mapped_column(
        ForeignKey("patterns.id", ondelete="CASCADE")
    )
    source_disk_id: Mapped[str] = mapped_column(String(36))
    source_vm_id: Mapped[str] = mapped_column(String(36))
    s3_key: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    virtual_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="uploading")

    pattern: Mapped["Pattern"] = relationship(back_populates="disks")


class PatternShare(Base):
    __tablename__ = "pattern_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern_id: Mapped[str] = mapped_column(
        ForeignKey("patterns.id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    pattern: Mapped["Pattern"] = relationship(back_populates="shares")
