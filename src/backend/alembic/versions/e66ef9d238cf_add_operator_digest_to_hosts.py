"""add operator_digest to hosts

Revision ID: e66ef9d238cf
Revises: da688a4c0eac
Create Date: 2026-07-23 13:28:54.507888

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e66ef9d238cf"
down_revision: str | Sequence[str] | None = "da688a4c0eac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("operator_digest", sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "operator_digest")
