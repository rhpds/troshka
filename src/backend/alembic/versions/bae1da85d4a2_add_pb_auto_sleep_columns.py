"""add pb auto sleep columns

Revision ID: bae1da85d4a2
Revises: 45e4854e140d
Create Date: 2026-06-16 19:42:30.108363

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "bae1da85d4a2"
down_revision: Union[str, Sequence[str], None] = "45e4854e140d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "storage_pools",
        sa.Column(
            "pb_auto_sleep_minutes",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "storage_pools",
        sa.Column("pb_last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("storage_pools", "pb_last_activity_at")
    op.drop_column("storage_pools", "pb_auto_sleep_minutes")
