import datetime
import json
import uuid

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    credentials: Mapped[str | None] = mapped_column(Text)
    default_region: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="active")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    hosts: Mapped[list["Host"]] = relationship(back_populates="provider")

    def get_credentials(self) -> dict:
        if not self.credentials:
            return {}
        return json.loads(self.credentials)

    def set_credentials(self, creds: dict):
        self.credentials = json.dumps(creds)
