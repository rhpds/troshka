"""Retain exact support operations and remote liabilities.

Revision ID: e06fd54ac283
Revises: d95ec43fb172
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e06fd54ac283"
down_revision = "d95ec43fb172"
branch_labels = None
depends_on = None

_TABLES = ("native_origin_ops_operations", "native_origin_ops_facts")


def _common():
    return [
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    ]


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    op.create_table(
        _TABLES[0],
        sa.Column("id", uuid, primary_key=True),
        sa.Column("intent_id", uuid, nullable=False, index=True),
        sa.Column("writer_id", uuid, nullable=False, index=True),
        sa.Column("encrypted_body", sa.Text(), nullable=True),
        *_common(),
    )
    op.create_table(
        _TABLES[1],
        sa.Column("id", sa.String(160), primary_key=True),
        sa.Column("operation_id", uuid, nullable=False, index=True),
        *_common(),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in _TABLES:
            op.execute(
                f"CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
            )


def downgrade():
    for table in reversed(_TABLES):
        op.drop_table(table)
