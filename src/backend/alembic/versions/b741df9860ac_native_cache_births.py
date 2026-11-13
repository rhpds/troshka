"""Protected new native cache creation journal, without historical adoption."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b741df9860ac"
down_revision = "8fbde773a642"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_cache_births",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("domain_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("intent_digest", sa.String(64), nullable=False),
        sa.Column("intent", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("create_response", postgresql.JSONB(), nullable=True),
        sa.Column("receipt", postgresql.JSONB(), nullable=True),
        sa.Column("receipt_digest", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_native_cache_births_domain_id", "native_cache_births", ["domain_id"]
    )


def downgrade():
    op.drop_table("native_cache_births")
