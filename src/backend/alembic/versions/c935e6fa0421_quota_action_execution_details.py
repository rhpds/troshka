"""Durable budget stop execution and notification evidence.

Revision ID: c935e6fa0421
Revises: b824d5ef9310
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c935e6fa0421"
down_revision = "b824d5ef9310"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "quota_actions",
        sa.Column(
            "details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("quota_actions", "details")
