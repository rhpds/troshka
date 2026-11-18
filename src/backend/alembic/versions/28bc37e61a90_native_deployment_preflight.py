"""Retain fresh-project proof and arbitrate pre-project-I/O recovery."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "28bc37e61a90"
down_revision = "f17ae65bd394"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    for table, key in (
        ("native_deployment_clean_baselines", "project_id"),
        ("native_deployment_preflights", "id"),
        ("native_deployment_preflight_decisions", "attempt_id"),
    ):
        identity = sa.Column(key, uuid, primary_key=True)
        if key == "attempt_id":
            identity = sa.Column(
                key,
                uuid,
                sa.ForeignKey("native_deployment_preflights.id"),
                primary_key=True,
            )
        columns = [
            identity,
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
        ]
        if key == "id":
            columns.append(sa.Column("project_id", uuid, nullable=False, index=True))
        op.create_table(table, *columns)
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                f"CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
            )
    op.create_table(
        "native_deployment_preflight_active",
        sa.Column("project_id", uuid, primary_key=True),
        sa.Column(
            "attempt_id",
            uuid,
            sa.ForeignKey("native_deployment_preflights.id"),
            nullable=False,
            unique=True,
        ),
    )


def downgrade():
    for table in (
        "native_deployment_preflight_active",
        "native_deployment_preflight_decisions",
        "native_deployment_preflights",
        "native_deployment_clean_baselines",
    ):
        op.drop_table(table)
