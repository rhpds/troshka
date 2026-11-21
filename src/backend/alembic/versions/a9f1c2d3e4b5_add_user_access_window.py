"""Add optional per-user access start/end window."""

import sqlalchemy as sa

from alembic import op

revision = "a9f1c2d3e4b5"
down_revision = "28bc37e61a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("access_starts_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("access_ends_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "access_ends_at")
    op.drop_column("users", "access_starts_at")
