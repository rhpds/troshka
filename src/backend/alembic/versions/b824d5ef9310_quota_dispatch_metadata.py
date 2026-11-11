"""Persist quota operation dispatch identity and recovery metadata.

Revision ID: b824d5ef9310
Revises: a713c4de8209
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b824d5ef9310"
down_revision = "a713c4de8209"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "quota_reservations",
        sa.Column(
            "dispatch_metadata", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
    )


def downgrade():
    op.drop_column("quota_reservations", "dispatch_metadata")
