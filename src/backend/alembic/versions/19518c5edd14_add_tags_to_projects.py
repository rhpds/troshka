"""add tags to projects

Revision ID: 19518c5edd14
Revises: c9186a2edfdb
Create Date: 2026-06-15 07:27:38.776828

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "19518c5edd14"
down_revision: str | Sequence[str] | None = "c9186a2edfdb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("tags", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "tags")
