"""Protected origin checkpoints; retained facts survive project deletion."""

from sqlalchemy import String, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NativeOriginCustodyRequest(Base):
    __tablename__ = "native_origin_custody_requests"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginCustodyReceipt(Base):
    __tablename__ = "native_origin_custody_receipts"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    project_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    domain_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    request_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class NativeOriginCustodyCurrent(Base):
    __tablename__ = "native_origin_custody_current"
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(UUID(as_uuid=False))


def _immutable(_mapper, _connection, _target):
    raise ValueError("Native origin custody facts are immutable")


for _model in (NativeOriginCustodyRequest, NativeOriginCustodyReceipt):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
