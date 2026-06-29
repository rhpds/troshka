"""add patterns snapshots tables

Revision ID: fa4247629ec7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-07 10:50:32.007285

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fa4247629ec7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "patterns",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "visibility", sa.String(20), nullable=False, server_default="private"
        ),
        sa.Column("source_project_id", sa.String(36), nullable=True),
        sa.Column("topology", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="creating"),
        sa.Column(
            "total_size_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    op.create_table(
        "pattern_disks",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "pattern_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("patterns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_disk_id", sa.String(36), nullable=False),
        sa.Column("source_vm_id", sa.String(36), nullable=False),
        sa.Column("s3_key", sa.String(500), nullable=False),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "virtual_size_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="uploading"),
    )

    op.create_table(
        "pattern_shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "pattern_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("patterns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    op.create_table(
        "library_item_disks",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "library_item_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("library_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("s3_key", sa.String(500), nullable=False),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "virtual_size_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("boot_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="uploading"),
    )

    op.add_column(
        "library_items",
        sa.Column("vm_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_column("library_items", "source_project_id")


def downgrade() -> None:
    op.add_column(
        "library_items", sa.Column("source_project_id", sa.String(36), nullable=True)
    )
    op.drop_column("library_items", "vm_config")
    op.drop_table("library_item_disks")
    op.drop_table("pattern_shares")
    op.drop_table("pattern_disks")
    op.drop_table("patterns")
