"""Retained immutable native deployment creation facts.

Revision ID: e12fc482a731
Revises: d90bc31e7426
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e12fc482a731"
down_revision = "d90bc31e7426"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=False)
    op.create_table(
        "native_deployment_origin_intents",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, nullable=False, index=True),
        sa.Column("attempt_id", uuid, nullable=False, unique=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    for table in (
        "native_deployment_namespace_receipts",
        "native_deployment_root_receipts",
    ):
        op.create_table(
            table,
            sa.Column("id", uuid, primary_key=True),
            sa.Column(
                "intent_id",
                uuid,
                sa.ForeignKey("native_deployment_origin_intents.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
        )
    op.create_table(
        "native_deployment_origin_active",
        sa.Column("project_id", uuid, primary_key=True),
        sa.Column(
            "intent_id",
            uuid,
            sa.ForeignKey("native_deployment_origin_intents.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("cancelled", sa.Boolean(), nullable=False),
        sa.Column("io_phase", sa.String(40), nullable=True),
        sa.Column("io_token", uuid, nullable=True),
        sa.Column("io_job_id", sa.String(100), nullable=True),
    )
    op.create_table(
        "native_deployment_origin_launches",
        sa.Column(
            "id",
            uuid,
            sa.ForeignKey("native_deployment_origin_intents.id"),
            primary_key=True,
        ),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_native_origin_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Native deployment origin facts are immutable'; END $$"
        )
        for table in (
            "native_deployment_origin_intents",
            "native_deployment_namespace_receipts",
            "native_deployment_root_receipts",
            "native_deployment_origin_launches",
        ):
            op.execute(
                f"CREATE TRIGGER immutable_native_origin BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_native_origin_mutation()"
            )


def downgrade():
    for table in (
        "native_deployment_origin_launches",
        "native_deployment_origin_active",
        "native_deployment_root_receipts",
        "native_deployment_namespace_receipts",
        "native_deployment_origin_intents",
    ):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_native_origin_mutation()")
