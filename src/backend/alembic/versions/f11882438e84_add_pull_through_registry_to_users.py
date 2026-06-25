"""add pull_through_registry to users

Revision ID: f11882438e84
Revises: b4acffe6bbec
Create Date: 2026-06-25 11:25:14.247794

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f11882438e84"
down_revision: Union[str, Sequence[str], None] = "b4acffe6bbec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column(
            "pull_through_registry",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    op.add_column(
        "users", sa.Column("pull_through_registry_url", sa.String(255), nullable=True)
    )
    op.add_column(
        "users", sa.Column("pull_through_registry_user", sa.String(255), nullable=True)
    )
    op.add_column(
        "users", sa.Column("pull_through_registry_password", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "pull_through_registry_password")
    op.drop_column("users", "pull_through_registry_user")
    op.drop_column("users", "pull_through_registry_url")
    op.drop_column("users", "pull_through_registry")
