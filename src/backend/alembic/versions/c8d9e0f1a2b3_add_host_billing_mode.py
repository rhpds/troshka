"""add host billing_mode for dedicated vs shared metering

Revision ID: c8d9e0f1a2b3
Revises: b7e8f9a0c1d2
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: str | Sequence[str] | None = "b7e8f9a0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column(
            "billing_mode",
            sa.String(20),
            nullable=False,
            server_default="shared",
        ),
    )


def downgrade() -> None:
    op.drop_column("hosts", "billing_mode")
