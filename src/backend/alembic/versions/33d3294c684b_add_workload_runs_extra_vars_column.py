"""add workload_runs extra_vars column

Revision ID: 33d3294c684b
Revises: eee0a961fc7b
Create Date: 2026-09-13 19:06:33.953920

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "33d3294c684b"
down_revision: str | Sequence[str] | None = "eee0a961fc7b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "workload_runs",
        sa.Column("extra_vars", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("workload_runs", "extra_vars")
