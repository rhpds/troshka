"""Retained managed CSI clone lineage, without historical adoption."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c83a6712fd90"
down_revision = "b741df9860ac"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_clone_lineages",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("operation_id", sa.String(256), nullable=False),
        sa.Column("claim_name", sa.String(256), nullable=False),
        sa.Column("domain_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("intent", postgresql.JSONB(), nullable=False),
        sa.Column("intent_digest", sa.String(64), nullable=False),
        sa.Column("creation", postgresql.JSONB(), nullable=True),
        sa.Column("proof", postgresql.JSONB(), nullable=True),
        sa.Column("proof_digest", sa.String(64), nullable=True),
        sa.UniqueConstraint("operation_id", "claim_name"),
    )
    op.create_index(
        "ix_native_clone_lineages_domain_id", "native_clone_lineages", ["domain_id"]
    )


def downgrade():
    op.drop_table("native_clone_lineages")
