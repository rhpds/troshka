"""Append-only reviewed native custody transitions.

Revision ID: c81fa62d9730
Revises: ab841d2e6109
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c81fa62d9730"
down_revision = "ab841d2e6109"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_custody_reconciliation_requests",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "native_custody_successors",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "predecessor_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            unique=True,
        ),
        sa.Column("predecessor_digest", sa.String(64), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_native_custody_successors_project_id",
        "native_custody_successors",
        ["project_id"],
    )


def downgrade():
    op.drop_table("native_custody_successors")
    op.drop_table("native_custody_reconciliation_requests")
