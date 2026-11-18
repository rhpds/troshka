"""Retain generic project namespace cleanup without a Project foreign key."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f17ae65bd394"
down_revision = "e06fd54ac283"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    for name, relation in (
        ("native_project_namespace_cleanups", "project_id"),
        ("native_project_namespace_evidence", "cleanup_id"),
    ):
        columns = [
            sa.Column("id", uuid, primary_key=True),
            sa.Column(relation, uuid, nullable=False, index=True),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
        ]
        if relation == "cleanup_id":
            columns += [
                sa.Column("phase", sa.String(100), nullable=False),
                sa.UniqueConstraint("cleanup_id", "phase"),
            ]
        op.create_table(name, *columns)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON native_project_namespace_evidence "
            "FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
        )


def downgrade():
    op.drop_table("native_project_namespace_evidence")
    op.drop_table("native_project_namespace_cleanups")
