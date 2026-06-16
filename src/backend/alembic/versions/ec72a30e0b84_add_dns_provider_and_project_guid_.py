"""add dns provider and project guid domain fields

Revision ID: ec72a30e0b84
Revises: 31a4e434ad56
Create Date: 2026-06-10 18:50:53.154058

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ec72a30e0b84"
down_revision: Union[str, Sequence[str], None] = "31a4e434ad56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "dns_providers",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.add_column("projects", sa.Column("guid", sa.String(50), nullable=True))
    op.add_column("projects", sa.Column("domain", sa.String(255), nullable=True))
    op.add_column(
        "projects",
        sa.Column(
            "dns_provider_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dns_providers.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projects", "dns_provider_id")
    op.drop_column("projects", "domain")
    op.drop_column("projects", "guid")
    op.drop_table("dns_providers")
