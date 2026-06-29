"""add source_url to library_items

Revision ID: 5f780b5709f9
Revises: 3fc2aa69be81
Create Date: 2026-06-14 20:41:11.914500

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f780b5709f9"
down_revision: str | Sequence[str] | None = "3fc2aa69be81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "library_items", sa.Column("source_url", sa.String(1000), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("library_items", "source_url")
