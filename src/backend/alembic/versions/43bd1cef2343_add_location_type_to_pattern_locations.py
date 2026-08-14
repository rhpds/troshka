"""add location_type to pattern_locations

Revision ID: 43bd1cef2343
Revises: 33827935d7e4
Create Date: 2026-08-13 17:08:08.866887

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "43bd1cef2343"
down_revision: str | Sequence[str] | None = "33827935d7e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade():
    op.add_column(
        "pattern_locations",
        sa.Column(
            "location_type",
            sa.String(length=20),
            nullable=False,
            server_default="obc",
        ),
    )
    op.alter_column(
        "pattern_locations",
        "provider_id",
        existing_type=postgresql.UUID(as_uuid=False),
        nullable=True,
    )


def downgrade():
    # NOTE: this restore of NOT NULL will fail if any central rows exist
    # (location_type="central" rows have provider_id=NULL). Delete/convert
    # those rows before downgrading.
    op.alter_column(
        "pattern_locations",
        "provider_id",
        existing_type=postgresql.UUID(as_uuid=False),
        nullable=False,
    )
    op.drop_column("pattern_locations", "location_type")
