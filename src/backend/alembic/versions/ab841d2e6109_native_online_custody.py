"""Durable explicit online native custody receipts.

Revision ID: ab841d2e6109
Revises: d92b51e7046a
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "ab841d2e6109"
down_revision = "d92b51e7046a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_custody_requests",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "native_custody_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=False), nullable=False, unique=True
        ),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )


def downgrade():
    op.drop_table("native_custody_receipts")
    op.drop_table("native_custody_requests")
