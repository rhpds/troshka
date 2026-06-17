"""add project auto timers

Revision ID: aa8a957d6c51
Revises: bae1da85d4a2
Create Date: 2026-06-17 12:51:55.583475

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "aa8a957d6c51"
down_revision: Union[str, Sequence[str], None] = "bae1da85d4a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column(
        "projects", sa.Column("auto_stop_minutes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column("auto_stop_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("auto_stop_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "auto_stop_warned", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.add_column(
        "projects", sa.Column("auto_delete_minutes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column("auto_delete_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "auto_delete_warned", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.drop_column("projects", "run_timer_hours")
    op.drop_column("projects", "run_timer_max_ext_hours")
    op.drop_column("projects", "run_timer_started_at")


def downgrade():
    op.add_column("projects", sa.Column("run_timer_hours", sa.Integer(), nullable=True))
    op.add_column(
        "projects", sa.Column("run_timer_max_ext_hours", sa.Integer(), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column("run_timer_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_column("projects", "auto_stop_minutes")
    op.drop_column("projects", "auto_stop_started_at")
    op.drop_column("projects", "auto_stop_expires_at")
    op.drop_column("projects", "auto_stop_warned")
    op.drop_column("projects", "auto_delete_minutes")
    op.drop_column("projects", "auto_delete_started_at")
    op.drop_column("projects", "auto_delete_warned")
