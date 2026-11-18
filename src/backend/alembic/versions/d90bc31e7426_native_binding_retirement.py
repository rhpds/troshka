"""Protected per-binding retirement evidence.

Revision ID: d90bc31e7426
Revises: c81fa62d9730
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d90bc31e7426"
down_revision = "c81fa62d9730"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_binding_retirement_requests",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "native_binding_retirement_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "request_id", postgresql.UUID(as_uuid=False), nullable=False, unique=True
        ),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_native_binding_retirement_receipts_project_id",
        "native_binding_retirement_receipts",
        ["project_id"],
    )


def downgrade():
    op.drop_table("native_binding_retirement_receipts")
    op.drop_table("native_binding_retirement_requests")
