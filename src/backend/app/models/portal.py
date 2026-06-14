import secrets
import uuid

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, backref, mapped_column, relationship

from app.core.database import Base


class ProjectPortalToken(Base):
    __tablename__ = "project_portal_tokens"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
        default=lambda: secrets.token_urlsafe(32),
    )
    access_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="readonly"
    )
    expires_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)

    project = relationship(
        "Project",
        backref=backref("portal_tokens", cascade="all, delete-orphan"),
    )
