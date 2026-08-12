"""add pattern_locations

Revision ID: a1b2c3d4e5f6
Revises: 67320038e4ea
Create Date: 2026-08-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "67320038e4ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pattern_locations",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "pattern_disk_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("pattern_disks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("s3_key", sa.String(500), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="syncing"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_pattern_locations_disk_provider",
        "pattern_locations",
        ["pattern_disk_id", "provider_id"],
        unique=True,
    )

    op.add_column(
        "patterns",
        sa.Column(
            "source_provider_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("providers.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("patterns", "source_provider_id")
    op.drop_index("ix_pattern_locations_disk_provider", table_name="pattern_locations")
    op.drop_table("pattern_locations")
