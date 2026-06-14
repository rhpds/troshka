"""add private ip to hosts

Revision ID: 31a4e434ad56
Revises: 350535bf9ed3
Create Date: 2026-06-10 17:32:07.989699

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "31a4e434ad56"
down_revision: Union[str, Sequence[str], None] = "350535bf9ed3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("private_ip", sa.String(45), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "private_ip")
