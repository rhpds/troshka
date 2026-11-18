"""Retain exact support handoffs and namespace-only cleanup evidence.

Revision ID: c84db32ea061
Revises: b73ca21d9e50
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c84db32ea061"
down_revision = "b73ca21d9e50"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    for table, key, extra in (
        (
            "native_origin_support_handoffs",
            "id",
            [sa.Column("intent_id", uuid, nullable=False, index=True)],
        ),
        ("native_origin_support_receipts", "handoff_id", []),
        (
            "native_origin_namespace_cleanups",
            "id",
            [
                sa.Column("intent_id", uuid, nullable=False, unique=True),
                sa.Column("project_id", uuid, nullable=False, index=True),
            ],
        ),
        (
            "native_origin_namespace_evidence",
            "id",
            [
                sa.Column("cleanup_id", uuid, nullable=False, index=True),
                sa.Column("phase", sa.String(40), nullable=False),
                sa.UniqueConstraint("cleanup_id", "phase"),
            ],
        ),
    ):
        op.create_table(
            table,
            sa.Column(key, uuid, primary_key=True),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
            *extra,
        )
        if (
            table != "native_origin_namespace_cleanups"
            and op.get_bind().dialect.name == "postgresql"
        ):
            op.execute(
                f"CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
            )


def downgrade():
    for table in (
        "native_origin_namespace_evidence",
        "native_origin_namespace_cleanups",
        "native_origin_support_receipts",
        "native_origin_support_handoffs",
    ):
        op.drop_table(table)
