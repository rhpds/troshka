"""add eip port_map and provider max_eips

Revision ID: 9050f4789cd3
Revises: eb84a572ce90
Create Date: 2026-06-16 08:03:48.493499

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "9050f4789cd3"
down_revision: Union[str, Sequence[str], None] = "eb84a572ce90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "elastic_ips", sa.Column("port_map", postgresql.JSONB(), nullable=True)
    )
    op.add_column("providers", sa.Column("max_eips", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("providers", "max_eips")
    op.drop_column("elastic_ips", "port_map")
