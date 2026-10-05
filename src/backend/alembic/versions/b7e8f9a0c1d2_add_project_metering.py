"""add project metering tables and budget columns

Revision ID: b7e8f9a0c1d2
Revises: a1c2e3f4b5d6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7e8f9a0c1d2"
down_revision: str | Sequence[str] | None = "a1c2e3f4b5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("budget_usd", sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "budget_warned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "budget_stopped",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "hosts",
        sa.Column(
            "metering_rates", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_table(
        "metering_rate_defaults",
        sa.Column("provider_type", sa.String(20), primary_key=True),
        sa.Column("rates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_table(
        "metering_intervals",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.String(100), nullable=False),
        sa.Column("qty", sa.Numeric(18, 6), nullable=False),
        sa.Column("unit_rate", sa.Numeric(18, 6), nullable=False),
        sa.Column("host_id", sa.String(36), nullable=True),
        sa.Column("provider_type", sa.String(20), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_metering_intervals_project_id", "metering_intervals", ["project_id"]
    )
    op.create_table(
        "project_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("project_name", sa.String(255), nullable=False),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("total_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("line_items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column(
            "finalized_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_project_invoices_project_id", "project_invoices", ["project_id"]
    )
    op.create_index("ix_project_invoices_owner_id", "project_invoices", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_project_invoices_owner_id", table_name="project_invoices")
    op.drop_index("ix_project_invoices_project_id", table_name="project_invoices")
    op.drop_table("project_invoices")
    op.drop_index("ix_metering_intervals_project_id", table_name="metering_intervals")
    op.drop_table("metering_intervals")
    op.drop_table("metering_rate_defaults")
    op.drop_column("hosts", "metering_rates")
    op.drop_column("projects", "budget_stopped")
    op.drop_column("projects", "budget_warned")
    op.drop_column("projects", "budget_usd")
