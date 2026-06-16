"""add pattern buffer columns to storage pools

Revision ID: 9e29d0fc6843
Revises: 19518c5edd14
Create Date: 2026-06-15 10:25:23.106140

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "9e29d0fc6843"
down_revision: Union[str, Sequence[str], None] = "19518c5edd14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "storage_pools",
        sa.Column("worker_host_id", postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.add_column(
        "storage_pools",
        sa.Column(
            "worker_instance_type",
            sa.String(50),
            nullable=True,
            server_default="c6id.xlarge",
        ),
    )
    op.create_foreign_key(
        "storage_pools_worker_host_id_fkey",
        "storage_pools",
        "hosts",
        ["worker_host_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "storage_pools_worker_host_id_fkey", "storage_pools", type_="foreignkey"
    )
    op.drop_column("storage_pools", "worker_instance_type")
    op.drop_column("storage_pools", "worker_host_id")
