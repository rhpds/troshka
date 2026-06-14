"""add console columns to providers

Revision ID: d8f5d6d8dce9
Revises: 962f65a3f579
Create Date: 2026-06-14 13:19:08.064310

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f5d6d8dce9"
down_revision: Union[str, Sequence[str], None] = "962f65a3f579"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "providers", sa.Column("console_zone_id", sa.String(100), nullable=True)
    )
    op.add_column(
        "providers", sa.Column("console_base_domain", sa.String(255), nullable=True)
    )
    op.add_column("providers", sa.Column("console_nameservers", JSONB, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("providers", "console_nameservers")
    op.drop_column("providers", "console_base_domain")
    op.drop_column("providers", "console_zone_id")
