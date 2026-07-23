"""add guest_exec_enabled to projects

Revision ID: 6cb96e6a5fab
Revises: 05ed40da618c
Create Date: 2026-07-12 17:55:28.894434

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6cb96e6a5fab"
down_revision: str | Sequence[str] | None = "05ed40da618c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "guest_exec_enabled", sa.Boolean(), server_default="true", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "guest_exec_enabled")
