"""add rh_offline_token to users

Revision ID: 45e4854e140d
Revises: 0e05ca4755c7
Create Date: 2026-06-16 17:47:34.203522

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "45e4854e140d"
down_revision: str | Sequence[str] | None = "0e05ca4755c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("rh_offline_token", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "rh_offline_token")
