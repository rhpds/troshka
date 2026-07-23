"""add ocp_status_detail to projects

Revision ID: 3ae4e810ade5
Revises: 50dd52c25702
Create Date: 2026-07-22 15:22:44.951871

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3ae4e810ade5"
down_revision: str | Sequence[str] | None = "50dd52c25702"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("ocp_status_detail", sa.String(200), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("projects", "ocp_status_detail")
