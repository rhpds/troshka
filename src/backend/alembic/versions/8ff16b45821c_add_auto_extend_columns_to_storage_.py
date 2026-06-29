"""add auto_extend columns to storage_pools and hosts

Revision ID: 8ff16b45821c
Revises: 3bb8922de96c
Create Date: 2026-06-14 08:56:43.924051

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ff16b45821c"
down_revision: str | Sequence[str] | None = "3bb8922de96c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "storage_pools",
        sa.Column(
            "auto_extend_enabled", sa.Boolean(), server_default="false", nullable=False
        ),
    )
    op.add_column(
        "storage_pools",
        sa.Column(
            "auto_extend_threshold_pct",
            sa.Integer(),
            server_default="80",
            nullable=False,
        ),
    )
    op.add_column(
        "storage_pools",
        sa.Column(
            "auto_extend_increment_gb",
            sa.Integer(),
            server_default="64",
            nullable=False,
        ),
    )
    op.add_column(
        "storage_pools", sa.Column("auto_extend_max_gb", sa.Integer(), nullable=True)
    )
    op.add_column(
        "hosts",
        sa.Column(
            "auto_extend_enabled", sa.Boolean(), server_default="false", nullable=False
        ),
    )
    op.add_column(
        "hosts",
        sa.Column(
            "auto_extend_threshold_pct",
            sa.Integer(),
            server_default="80",
            nullable=False,
        ),
    )
    op.add_column(
        "hosts",
        sa.Column(
            "auto_extend_increment_gb",
            sa.Integer(),
            server_default="100",
            nullable=False,
        ),
    )
    op.add_column("hosts", sa.Column("auto_extend_max_gb", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("hosts", "auto_extend_max_gb")
    op.drop_column("hosts", "auto_extend_increment_gb")
    op.drop_column("hosts", "auto_extend_threshold_pct")
    op.drop_column("hosts", "auto_extend_enabled")
    op.drop_column("storage_pools", "auto_extend_max_gb")
    op.drop_column("storage_pools", "auto_extend_increment_gb")
    op.drop_column("storage_pools", "auto_extend_threshold_pct")
    op.drop_column("storage_pools", "auto_extend_enabled")
