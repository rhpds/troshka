from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.library import Library
    from app.models.project import Project


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    auth_source: Mapped[str] = mapped_column(String(20), default="local")
    password_hash: Mapped[str | None] = mapped_column(String(255))
    quota_overrides: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ocp_pull_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    pull_through_registry: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    pull_through_registry_url: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    pull_through_registry_user: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    pull_through_registry_password: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    rh_offline_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    projects: Mapped[list[Project]] = relationship(back_populates="owner")
    libraries: Mapped[list[Library]] = relationship(back_populates="owner")
    ssh_keys: Mapped[list[UserSshKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSshKey(Base):
    __tablename__ = "user_ssh_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    public_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="ssh_keys")
