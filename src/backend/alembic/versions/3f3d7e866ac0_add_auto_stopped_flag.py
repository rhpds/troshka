"""add auto_stopped flag

Revision ID: 3f3d7e866ac0
Revises: aa8a957d6c51
Create Date: 2026-06-17 14:18:50.063592

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3f3d7e866ac0"
down_revision: Union[str, Sequence[str], None] = "aa8a957d6c51"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("auto_stopped", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("projects", "auto_stopped")
