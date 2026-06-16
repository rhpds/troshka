"""add storage_warnings to hosts

Revision ID: 3bb8922de96c
Revises: f4a772faef01
Create Date: 2026-06-14 08:47:24.917257

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3bb8922de96c"
down_revision: Union[str, Sequence[str], None] = "f4a772faef01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("storage_warnings", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "storage_warnings")
