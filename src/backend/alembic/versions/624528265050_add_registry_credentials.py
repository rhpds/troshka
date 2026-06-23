"""add registry credentials

Revision ID: 624528265050
Revises: 3f3d7e866ac0
Create Date: 2026-06-22 19:03:28.486501

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "624528265050"
down_revision: Union[str, Sequence[str], None] = "3f3d7e866ac0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "registry_credentials",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("registry", sa.String(255), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("registry_credentials")
