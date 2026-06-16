"""add nfs_port to storage_pools

Revision ID: 88043a868aa7
Revises: 9050f4789cd3
Create Date: 2026-06-16 11:48:12.333481

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "88043a868aa7"
down_revision: Union[str, Sequence[str], None] = "9050f4789cd3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("storage_pools", sa.Column("nfs_port", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("storage_pools", "nfs_port")
