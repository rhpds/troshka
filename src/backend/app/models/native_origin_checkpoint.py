"""Retained producer requests and private signing keys; no project cascade."""

from sqlalchemy import String, Text, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NativeOriginCheckpointKey(Base):
    __tablename__ = "native_origin_checkpoint_keys"
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    public_key: Mapped[str] = mapped_column(String(100))
    encrypted_private_key: Mapped[str] = mapped_column(Text)


class NativeOriginProducerReceipt(Base):
    __tablename__ = "native_origin_producer_receipts"
    request_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    intent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    custody_receipt_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    custody_digest: Mapped[str] = mapped_column(String(64))
    predecessor_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )


def _immutable(_mapper, _connection, _target):
    raise ValueError("Native origin producer facts are immutable")


for _model in (NativeOriginCheckpointKey, NativeOriginProducerReceipt):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
