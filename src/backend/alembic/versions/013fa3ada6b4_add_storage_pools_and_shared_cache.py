"""add storage pools and shared cache

Revision ID: 013fa3ada6b4
Revises: 0642f947d40f
Create Date: 2026-06-10 13:46:08.485256

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "013fa3ada6b4"
down_revision: str | Sequence[str] | None = "0642f947d40f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "storage_pools",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("az", sa.String(50), nullable=True),
        sa.Column("subnet_id", sa.String(50), nullable=True),
        sa.Column("fsx_filesystem_id", sa.String(50), nullable=True),
        sa.Column("fsx_dns_name", sa.String(255), nullable=True),
        sa.Column("fsx_mount_ip", sa.String(45), nullable=True),
        sa.Column("fsx_throughput_mbps", sa.Integer(), nullable=True),
        sa.Column("fsx_storage_gb", sa.Integer(), nullable=True),
        sa.Column("nfs_endpoint", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="creating"),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("providers.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    op.create_table(
        "shared_cache_entries",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "storage_pool_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("storage_pools.id"),
            nullable=False,
        ),
        sa.Column("item_type", sa.String(20), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="downloading"
        ),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("downloaded_by_host_id", sa.String(36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    op.add_column(
        "hosts",
        sa.Column(
            "storage_pool_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("storage_pools.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("hosts", "storage_pool_id")
    op.drop_table("shared_cache_entries")
    op.drop_table("storage_pools")
