"""add clock_target to projects

Revision ID: 06a1ab2607a5
Revises: 624528265050
Create Date: 2026-06-23 14:18:17.406843

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "06a1ab2607a5"
down_revision: str | Sequence[str] | None = "624528265050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projects",
        sa.Column("clock_target", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projects", "clock_target")
