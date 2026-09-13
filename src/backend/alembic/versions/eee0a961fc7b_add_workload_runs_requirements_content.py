"""add workload_runs.requirements_content

Revision ID: eee0a961fc7b
Revises: 6dbae6490f27
Create Date: 2026-09-13 14:37:08.461306

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eee0a961fc7b"
down_revision: str | Sequence[str] | None = "6dbae6490f27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "workload_runs",
        sa.Column("requirements_content", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("workload_runs", "requirements_content")
