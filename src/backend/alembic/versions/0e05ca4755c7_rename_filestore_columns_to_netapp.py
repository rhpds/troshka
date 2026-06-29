"""rename filestore columns to netapp

Revision ID: 0e05ca4755c7
Revises: 99cc69011416
Create Date: 2026-06-16 17:00:07.002545

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0e05ca4755c7"
down_revision: str | Sequence[str] | None = "99cc69011416"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "storage_pools", "filestore_instance_id", new_column_name="netapp_pool_id"
    )
    op.alter_column("storage_pools", "filestore_ip", new_column_name="netapp_mount_ip")
    op.alter_column(
        "storage_pools", "filestore_share_name", new_column_name="netapp_volume_name"
    )
    op.alter_column(
        "storage_pools", "filestore_tier", new_column_name="netapp_service_level"
    )
    op.alter_column(
        "storage_pools", "filestore_capacity_gb", new_column_name="netapp_capacity_gb"
    )


def downgrade() -> None:
    op.alter_column(
        "storage_pools", "netapp_pool_id", new_column_name="filestore_instance_id"
    )
    op.alter_column("storage_pools", "netapp_mount_ip", new_column_name="filestore_ip")
    op.alter_column(
        "storage_pools", "netapp_volume_name", new_column_name="filestore_share_name"
    )
    op.alter_column(
        "storage_pools", "netapp_service_level", new_column_name="filestore_tier"
    )
    op.alter_column(
        "storage_pools", "netapp_capacity_gb", new_column_name="filestore_capacity_gb"
    )
