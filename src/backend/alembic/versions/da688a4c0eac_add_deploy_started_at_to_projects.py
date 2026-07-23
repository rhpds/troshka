"""add deploy_started_at to projects

Revision ID: da688a4c0eac
Revises: f0e1e671373c
Create Date: 2026-07-23 09:07:04.781905

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "da688a4c0eac"
down_revision: str | Sequence[str] | None = "f0e1e671373c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("deploy_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "deploy_started_at")
