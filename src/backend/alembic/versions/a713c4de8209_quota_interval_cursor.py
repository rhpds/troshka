"""Retain locked interval accrual cursors and attribution snapshots.

Revision ID: a713c4de8209
Revises: f62c48d91a07
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a713c4de8209"
down_revision = "f62c48d91a07"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "metering_intervals",
        sa.Column("quota_accrued_through", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "metering_intervals",
        sa.Column("quota_attribution", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("metering_intervals", "quota_attribution")
    op.drop_column("metering_intervals", "quota_accrued_through")
