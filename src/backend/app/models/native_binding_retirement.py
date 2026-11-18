"""Protected append-only binding retirement requests and verified receipts."""

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NativeBindingRetirementRequest(Base):
    __tablename__ = "native_binding_retirement_requests"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeBindingRetirementReceipt(Base):
    __tablename__ = "native_binding_retirement_receipts"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    request_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
