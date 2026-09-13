"""add ocp control plane usable milestone fields

Revision ID: 6dbae6490f27
Revises: 18188b5d863f
Create Date: 2026-09-13 12:38:55.692211

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6dbae6490f27"
down_revision: str | Sequence[str] | None = "18188b5d863f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projects",
        sa.Column(
            "ocp_control_plane_usable_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "projects",
        sa.Column("ocp_control_plane_usable_elapsed", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projects", "ocp_control_plane_usable_elapsed")
    op.drop_column("projects", "ocp_control_plane_usable_at")
