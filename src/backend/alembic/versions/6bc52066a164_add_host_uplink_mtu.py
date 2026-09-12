"""add host uplink_mtu

Revision ID: 6bc52066a164
Revises: f0e20e48f32d
Create Date: 2026-09-11 19:30:39.827803

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6bc52066a164"
down_revision: str | Sequence[str] | None = "f0e20e48f32d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("hosts", sa.Column("uplink_mtu", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("hosts", "uplink_mtu")
