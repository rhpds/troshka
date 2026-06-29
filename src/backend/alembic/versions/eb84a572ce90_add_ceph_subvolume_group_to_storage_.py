"""add ceph subvolume group to storage pools

Revision ID: eb84a572ce90
Revises: 9e29d0fc6843
Create Date: 2026-06-15 17:34:10.330156

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eb84a572ce90"
down_revision: str | Sequence[str] | None = "9e29d0fc6843"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storage_pools",
        sa.Column("ceph_subvolume_group", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storage_pools", "ceph_subvolume_group")
