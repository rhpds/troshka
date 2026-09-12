"""add workload_runs

Revision ID: 18188b5d863f
Revises: 6bc52066a164
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "18188b5d863f"
down_revision = "6bc52066a164"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workload_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("owner_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("catalog_item", sa.String(255), nullable=True),
        sa.Column("role_fqcn", sa.String(255), nullable=True),
        sa.Column("scm_ref", sa.String(255), nullable=True),
        sa.Column("ee_image", sa.String(512), nullable=True),
        sa.Column("target_map", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("log_ref", sa.Text, nullable=True),
        sa.Column(
            "resulting_pattern_id", postgresql.UUID(as_uuid=False), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_workload_runs_project_id", "workload_runs", ["project_id"])


def downgrade():
    op.drop_index("ix_workload_runs_project_id", table_name="workload_runs")
    op.drop_table("workload_runs")
