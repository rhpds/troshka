"""add gcp and azure provider and storage pool columns

Revision ID: 8d54bbd22bdc
Revises: 157d0b22ab2b
Create Date: 2026-06-16 14:25:27.677218

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d54bbd22bdc"
down_revision: str | Sequence[str] | None = "157d0b22ab2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Provider — GCP columns
    op.add_column(
        "providers", sa.Column("gcp_project_id", sa.String(100), nullable=True)
    )
    op.add_column(
        "providers", sa.Column("gcp_network_id", sa.String(255), nullable=True)
    )
    op.add_column(
        "providers", sa.Column("gcp_subnet_id", sa.String(255), nullable=True)
    )
    op.add_column(
        "providers", sa.Column("gcp_firewall_policy", sa.String(255), nullable=True)
    )
    op.add_column("providers", sa.Column("gcp_zone", sa.String(50), nullable=True))

    # Provider — Azure columns
    op.add_column(
        "providers", sa.Column("azure_subscription_id", sa.String(50), nullable=True)
    )
    op.add_column(
        "providers", sa.Column("azure_resource_group", sa.String(100), nullable=True)
    )
    op.add_column(
        "providers", sa.Column("azure_vnet_id", sa.String(255), nullable=True)
    )
    op.add_column(
        "providers", sa.Column("azure_subnet_id", sa.String(255), nullable=True)
    )
    op.add_column("providers", sa.Column("azure_nsg_id", sa.String(255), nullable=True))
    op.add_column(
        "providers", sa.Column("azure_location", sa.String(50), nullable=True)
    )

    # StoragePool — GCP Filestore columns
    op.add_column(
        "storage_pools",
        sa.Column("filestore_instance_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "storage_pools", sa.Column("filestore_ip", sa.String(45), nullable=True)
    )
    op.add_column(
        "storage_pools",
        sa.Column("filestore_share_name", sa.String(100), nullable=True),
    )
    op.add_column(
        "storage_pools", sa.Column("filestore_tier", sa.String(20), nullable=True)
    )
    op.add_column(
        "storage_pools", sa.Column("filestore_capacity_gb", sa.Integer(), nullable=True)
    )

    # StoragePool — Azure Files NFS columns
    op.add_column(
        "storage_pools",
        sa.Column("azure_storage_account", sa.String(100), nullable=True),
    )
    op.add_column(
        "storage_pools",
        sa.Column("azure_file_share_name", sa.String(100), nullable=True),
    )
    op.add_column(
        "storage_pools",
        sa.Column("azure_file_share_url", sa.String(500), nullable=True),
    )
    op.add_column(
        "storage_pools",
        sa.Column("azure_files_capacity_gb", sa.Integer(), nullable=True),
    )
    op.add_column(
        "storage_pools", sa.Column("azure_files_iops", sa.Integer(), nullable=True)
    )
    op.add_column(
        "storage_pools",
        sa.Column("azure_files_throughput", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # StoragePool — Azure Files
    op.drop_column("storage_pools", "azure_files_throughput")
    op.drop_column("storage_pools", "azure_files_iops")
    op.drop_column("storage_pools", "azure_files_capacity_gb")
    op.drop_column("storage_pools", "azure_file_share_url")
    op.drop_column("storage_pools", "azure_file_share_name")
    op.drop_column("storage_pools", "azure_storage_account")

    # StoragePool — Filestore
    op.drop_column("storage_pools", "filestore_capacity_gb")
    op.drop_column("storage_pools", "filestore_tier")
    op.drop_column("storage_pools", "filestore_share_name")
    op.drop_column("storage_pools", "filestore_ip")
    op.drop_column("storage_pools", "filestore_instance_id")

    # Provider — Azure
    op.drop_column("providers", "azure_location")
    op.drop_column("providers", "azure_nsg_id")
    op.drop_column("providers", "azure_subnet_id")
    op.drop_column("providers", "azure_vnet_id")
    op.drop_column("providers", "azure_resource_group")
    op.drop_column("providers", "azure_subscription_id")

    # Provider — GCP
    op.drop_column("providers", "gcp_zone")
    op.drop_column("providers", "gcp_firewall_policy")
    op.drop_column("providers", "gcp_subnet_id")
    op.drop_column("providers", "gcp_network_id")
    op.drop_column("providers", "gcp_project_id")
