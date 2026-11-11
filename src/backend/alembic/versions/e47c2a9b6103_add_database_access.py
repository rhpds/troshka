"""Add database-controlled access; legacy accounts require one-time import."""

import sqlalchemy as sa

from alembic import op

revision = "e47c2a9b6103"
down_revision = "d93b8a6c1042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "access_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # Existing rows stay disabled until their prior authorization is verified.
    # New rows created by an admin are enabled by default.
    with op.batch_alter_table("users") as batch:
        batch.alter_column("access_enabled", server_default=sa.true())


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("access_enabled")
