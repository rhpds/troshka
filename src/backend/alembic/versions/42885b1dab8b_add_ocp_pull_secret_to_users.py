"""add ocp_pull_secret to users

Revision ID: 42885b1dab8b
Revises: ec72a30e0b84
Create Date: 2026-06-11 11:19:16.499945

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "42885b1dab8b"
down_revision: Union[str, Sequence[str], None] = "ec72a30e0b84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("ocp_pull_secret", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "ocp_pull_secret")
