"""add_project_off_action

Revision ID: c82589e4261c
Revises: d9e0f1a2b3c4
Create Date: 2026-10-07 15:09:16.771744

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c82589e4261c"
down_revision: str | Sequence[str] | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("off_action", sa.String(20), server_default="stop", nullable=False),
    )
    op.add_column(
        "projects",
        sa.Column(
            "power_warn_dismissed",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "power_warn_dismissed")
    op.drop_column("projects", "off_action")
