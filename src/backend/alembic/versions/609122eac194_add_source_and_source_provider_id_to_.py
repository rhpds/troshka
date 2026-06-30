"""add source and source_provider_id to library_items

Revision ID: 609122eac194
Revises: f11882438e84
Create Date: 2026-06-30 18:22:01.813159

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "609122eac194"
down_revision: Union[str, Sequence[str], None] = "f11882438e84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "library_items",
        sa.Column("source", sa.String(20), server_default="local", nullable=False),
    )
    op.add_column(
        "library_items",
        sa.Column(
            "source_provider_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("providers.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("library_items", "source_provider_id")
    op.drop_column("library_items", "source")
