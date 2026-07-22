"""add ocp_monitor_started_at to projects

Revision ID: f0e1e671373c
Revises: 3ae4e810ade5
Create Date: 2026-07-22 15:28:47.661362

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f0e1e671373c"
down_revision: Union[str, Sequence[str], None] = "3ae4e810ade5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("ocp_monitor_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "ocp_monitor_started_at")
