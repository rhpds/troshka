from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.host import Host
    from app.models.project import Project


class VM(Base):
    __tablename__ = "vms"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000))
    vcpus: Mapped[int] = mapped_column(Integer, default=2)
    ram_mb: Mapped[int] = mapped_column(Integer, default=4096)
    os_template: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="stopped")
    boot_method: Mapped[str] = mapped_column(String(20), default="template")
    boot_iso_id: Mapped[str | None] = mapped_column(ForeignKey("library_items.id"))
    pxe_profile_id: Mapped[str | None] = mapped_column(String(36))
    boot_order: Mapped[int] = mapped_column(Integer, default=0)
    console_type: Mapped[str] = mapped_column(String(10), default="auto")
    cloud_init: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    mac_address: Mapped[str | None] = mapped_column(String(17))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="vms")
    host: Mapped["Host | None"] = relationship(back_populates="vms")
    interfaces: Mapped[list["VMInterface"]] = relationship(
        back_populates="vm", cascade="all, delete-orphan"
    )
    prereqs: Mapped[list["BootPrereq"]] = relationship(
        back_populates="vm",
        foreign_keys="BootPrereq.vm_id",
        cascade="all, delete-orphan",
    )


class BootPrereq(Base):
    __tablename__ = "boot_prereqs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vm_id: Mapped[str] = mapped_column(ForeignKey("vms.id", ondelete="CASCADE"))
    depends_on_vm_id: Mapped[str] = mapped_column(ForeignKey("vms.id"))
    check_type: Mapped[str] = mapped_column(String(20), default="none")
    check_value: Mapped[str | None] = mapped_column(String(100))

    vm: Mapped["VM"] = relationship(back_populates="prereqs", foreign_keys=[vm_id])


class VMInterface(Base):
    __tablename__ = "vm_interfaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vm_id: Mapped[str] = mapped_column(ForeignKey("vms.id", ondelete="CASCADE"))
    network_id: Mapped[str] = mapped_column(ForeignKey("networks.id"))
    ip_mode: Mapped[str] = mapped_column(String(10), default="dhcp")
    ip_address: Mapped[str | None] = mapped_column(String(45))
    mac_address: Mapped[str | None] = mapped_column(String(17))
    dns_name: Mapped[str | None] = mapped_column(String(255))

    vm: Mapped["VM"] = relationship(back_populates="interfaces")
