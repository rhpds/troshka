"""add storage_size_gb to hosts

Revision ID: a1b2c3d4e5f6
Revises: dbf91986fccb
Create Date: 2026-06-06 14:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "c6db35dc1084"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column(
            "storage_size_gb", sa.Integer(), nullable=False, server_default="500"
        ),
    )


def downgrade() -> None:
    op.drop_column("hosts", "storage_size_gb")
