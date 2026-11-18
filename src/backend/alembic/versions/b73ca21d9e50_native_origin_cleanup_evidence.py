"""Retain writer termination and monotonic root submission history.

Revision ID: b73ca21d9e50
Revises: a62f19cb0e47
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b73ca21d9e50"
down_revision = "a62f19cb0e47"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    op.create_table(
        "native_origin_writers",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("intent_id", uuid, nullable=False, index=True),
        sa.Column("execution_id", sa.String(200), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("intent_id", "execution_id"),
    )
    for table, key in (
        ("native_origin_writer_terminals", "writer_id"),
        ("native_origin_writer_holds", "writer_id"),
        ("native_origin_root_submissions", "intent_id"),
    ):
        op.create_table(
            table,
            sa.Column(key, uuid, primary_key=True),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
        )
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "native_origin_writers",
            "native_origin_writer_terminals",
            "native_origin_writer_holds",
            "native_origin_root_submissions",
        ):
            op.execute(
                f"CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
            )


def downgrade():
    for table in (
        "native_origin_root_submissions",
        "native_origin_writer_holds",
        "native_origin_writer_terminals",
        "native_origin_writers",
    ):
        op.drop_table(table)
