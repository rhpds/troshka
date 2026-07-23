"""add system_config table

Revision ID: 50dd52c25702
Revises: 6cb96e6a5fab
Create Date: 2026-07-16 14:58:01.447272

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "50dd52c25702"
down_revision: str | Sequence[str] | None = "6cb96e6a5fab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_config",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("system_config")
