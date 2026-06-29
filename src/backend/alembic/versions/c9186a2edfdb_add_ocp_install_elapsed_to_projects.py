"""add ocp_install_elapsed to projects

Revision ID: c9186a2edfdb
Revises: 5f780b5709f9
Create Date: 2026-06-15 07:10:05.078838

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9186a2edfdb"
down_revision: str | Sequence[str] | None = "5f780b5709f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("ocp_install_elapsed", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("projects", "ocp_install_elapsed")
