"""Retain exact descendant dispatch closure and separate support settlement.

Revision ID: d95ec43fb172
Revises: c84db32ea061
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d95ec43fb172"
down_revision = "c84db32ea061"
branch_labels = None
depends_on = None

_KEYS = (
    ("native_origin_descendant_claims", "handoff_id"),
    ("native_origin_writer_closures", "writer_id"),
    ("native_origin_hold_resolutions", "writer_id"),
    ("native_origin_dispatch_closures", "intent_id"),
    ("native_origin_remote_holds", "writer_id"),
)


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    op.create_table(
        "native_origin_descendant_handoffs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("intent_id", uuid, nullable=False, index=True),
        sa.Column("parent_id", uuid, nullable=True, index=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    for table, key in _KEYS:
        op.create_table(
            table,
            sa.Column(key, uuid, primary_key=True),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
        )
    if op.get_bind().dialect.name == "postgresql":
        for table in ["native_origin_descendant_handoffs", *[t for t, _ in _KEYS]]:
            op.execute(
                f"CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
            )


def downgrade():
    for table in [
        *[t for t, _ in reversed(_KEYS)],
        "native_origin_descendant_handoffs",
    ]:
        op.drop_table(table)
