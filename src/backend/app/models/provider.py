import datetime
import json
import uuid

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    credentials: Mapped[str | None] = mapped_column(Text)
    default_region: Mapped[str | None] = mapped_column(String(100))
    default_image: Mapped[str | None] = mapped_column(String(500))
    vpc_id: Mapped[str | None] = mapped_column(String(50))
    subnet_id: Mapped[str | None] = mapped_column(String(50))
    security_group_id: Mapped[str | None] = mapped_column(String(50))
    console_zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    console_base_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    console_nameservers: Mapped[list | None] = mapped_column(JSONB, default=None)
    state: Mapped[str] = mapped_column(String(20), default="active")
    max_eips: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # GCP-specific
    gcp_project_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gcp_network_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gcp_subnet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gcp_firewall_policy: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gcp_zone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Azure-specific
    azure_subscription_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    azure_resource_group: Mapped[str | None] = mapped_column(String(100), nullable=True)
    azure_vnet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    azure_subnet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    azure_nsg_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    azure_location: Mapped[str | None] = mapped_column(String(50), nullable=True)

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
