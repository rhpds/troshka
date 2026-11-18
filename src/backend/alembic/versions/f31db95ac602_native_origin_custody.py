"""Retain protected origin storage checkpoints.

Revision ID: f31db95ac602
Revises: e12fc482a731
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f31db95ac602"
down_revision = "e12fc482a731"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    op.create_table(
        "native_origin_custody_requests",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "native_origin_custody_receipts",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("intent_id", uuid, nullable=False, index=True),
        sa.Column("project_id", uuid, nullable=False, index=True),
        sa.Column("domain_id", uuid, nullable=False, index=True),
        sa.Column("request_id", uuid, nullable=False, unique=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "native_origin_custody_current",
        sa.Column("intent_id", uuid, primary_key=True),
        sa.Column("receipt_id", uuid, nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "native_origin_custody_requests",
            "native_origin_custody_receipts",
        ):
            op.execute(
                f"CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
            )


def downgrade():
    for table in (
        "native_origin_custody_current",
        "native_origin_custody_receipts",
        "native_origin_custody_requests",
    ):
        op.drop_table(table)
