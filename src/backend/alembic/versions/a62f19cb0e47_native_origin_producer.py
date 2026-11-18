"""Retain signed origin producer receipt inputs and encrypted keys.

Revision ID: a62f19cb0e47
Revises: f31db95ac602
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a62f19cb0e47"
down_revision = "f31db95ac602"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    op.create_table(
        "native_origin_checkpoint_keys",
        sa.Column("intent_id", uuid, primary_key=True),
        sa.Column("public_key", sa.String(100), nullable=False),
        sa.Column("encrypted_private_key", sa.Text(), nullable=False),
    )
    op.create_table(
        "native_origin_producer_receipts",
        sa.Column("request_id", uuid, primary_key=True),
        sa.Column("intent_id", uuid, nullable=False, index=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("custody_receipt_id", uuid, nullable=False),
        sa.Column("custody_digest", sa.String(64), nullable=False),
        sa.Column("predecessor_id", uuid, nullable=True),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "native_origin_checkpoint_keys",
            "native_origin_producer_receipts",
        ):
            op.execute(
                f"CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
            )


def downgrade():
    op.drop_table("native_origin_producer_receipts")
    op.drop_table("native_origin_checkpoint_keys")
