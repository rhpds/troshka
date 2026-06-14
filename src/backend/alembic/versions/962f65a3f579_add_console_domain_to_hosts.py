"""add console_domain to hosts

Revision ID: 962f65a3f579
Revises: 8ff16b45821c
Create Date: 2026-06-14 10:57:29.870040

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "962f65a3f579"
down_revision: Union[str, Sequence[str], None] = "8ff16b45821c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("hosts", sa.Column("console_domain", sa.String(255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("hosts", "console_domain")
