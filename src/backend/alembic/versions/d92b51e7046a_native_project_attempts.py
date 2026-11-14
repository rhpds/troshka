"""Add isolated native collection attempt cursors."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d92b51e7046a"
down_revision = "c83a6712fd90"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_shared_storage_observations",
        sa.Column("domain_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("digest", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
    )
    op.create_table(
        "native_shared_observations",
        sa.Column("domain_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("observation_digest", sa.String(64), nullable=True),
    )
    op.create_table(
        "native_project_attempts",
        sa.Column("domain_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("root_key", sa.String(64), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("latest_report_id", postgresql.UUID(as_uuid=False)),
        sa.Column("latest_started_at", sa.DateTime(timezone=True)),
        sa.Column(
            "conflicted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade():
    op.drop_table("native_shared_storage_observations")
    op.drop_table("native_project_attempts")
    op.drop_table("native_shared_observations")
