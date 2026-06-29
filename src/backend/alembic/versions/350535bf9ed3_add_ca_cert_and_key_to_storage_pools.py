"""add ca cert and key to storage pools

Revision ID: 350535bf9ed3
Revises: 013fa3ada6b4
Create Date: 2026-06-10 17:02:55.511778

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "350535bf9ed3"
down_revision: str | Sequence[str] | None = "013fa3ada6b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("storage_pools", sa.Column("ca_cert", sa.Text(), nullable=True))
    op.add_column("storage_pools", sa.Column("ca_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("storage_pools", "ca_key")
    op.drop_column("storage_pools", "ca_cert")
