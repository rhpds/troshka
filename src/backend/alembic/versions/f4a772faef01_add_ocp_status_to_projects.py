"""add ocp_status to projects

Revision ID: f4a772faef01
Revises: 42885b1dab8b
Create Date: 2026-06-13 10:00:17.490908

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a772faef01"
down_revision: Union[str, Sequence[str], None] = "42885b1dab8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("ocp_status", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "ocp_status")
