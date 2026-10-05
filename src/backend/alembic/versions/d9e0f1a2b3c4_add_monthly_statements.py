"""add monthly_statements for metering rollups

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | Sequence[str] | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monthly_statements",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("line_items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("currency", sa.String(8), server_default="USD", nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "owner_id", "period_start", name="uq_monthly_statements_owner_period"
        ),
    )
    op.create_index(
        "ix_monthly_statements_owner_id", "monthly_statements", ["owner_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_monthly_statements_owner_id", table_name="monthly_statements")
    op.drop_table("monthly_statements")
