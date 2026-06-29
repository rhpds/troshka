"""add clock_target to patterns

Revision ID: b4acffe6bbec
Revises: 06a1ab2607a5
Create Date: 2026-06-23 15:24:51.535832

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4acffe6bbec"
down_revision: str | Sequence[str] | None = "06a1ab2607a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patterns",
        sa.Column("clock_target", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("patterns", "clock_target")
