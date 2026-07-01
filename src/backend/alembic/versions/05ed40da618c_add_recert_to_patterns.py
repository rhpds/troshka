"""add recert to patterns

Revision ID: 05ed40da618c
Revises: 609122eac194
Create Date: 2026-07-01 09:45:02.993654

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "05ed40da618c"
down_revision: str | Sequence[str] | None = "609122eac194"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patterns",
        sa.Column("recert", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("patterns", "recert")
