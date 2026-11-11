"""native storage reporting enrollment and evidence

Revision ID: 7eacbdd6fe04
Revises: c935e6fa0421
Create Date: 2026-11-11 17:28:07.960212

"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "7eacbdd6fe04"
down_revision = "c935e6fa0421"
branch_labels = None
depends_on = None


def _uuid(name, **kwargs):
    return sa.Column(name, postgresql.UUID(as_uuid=False), nullable=False, **kwargs)


def _json(name):
    return sa.Column(name, postgresql.JSONB(), nullable=False)


def _digest(name="digest", **kwargs):
    return sa.Column(name, sa.String(64), nullable=False, **kwargs)


def _domain_index(table):
    op.create_index(f"ix_{table}_domain_id", table, ["domain_id"])


def upgrade() -> None:
    """Reporting metadata only. No live object FKs, quota edits or backfill."""
    op.create_table(
        "native_reporting_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        _uuid("target_id", unique=True),
    )
    op.create_table(
        "native_enrollment_requests",
        _uuid("id", primary_key=True),
        _digest(),
        _json("payload"),
    )
    op.create_table(
        "native_enrollments",
        _uuid("id", primary_key=True),
        _uuid("domain_id"),
        _digest(unique=True),
        _json("payload"),
    )
    _domain_index("native_enrollments")
    op.create_table(
        "native_collection_domains",
        _uuid("id", primary_key=True),
        _uuid("enrollment_id"),
        sa.Column("latest_report_id", postgresql.UUID(as_uuid=False)),
        sa.Column("latest_started_at", sa.DateTime(timezone=True)),
        sa.Column("conflicted", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "native_pool_claims",
        _digest("key", primary_key=True),
        _uuid("domain_id"),
        _json("payload"),
    )
    _domain_index("native_pool_claims")
    op.create_table(
        "native_project_roots",
        _digest("key", primary_key=True),
        _uuid("domain_id"),
        _uuid("project_id"),
        _uuid("provider_id"),
        _uuid("enrollment_id"),
        _json("payload"),
    )
    _domain_index("native_project_roots")
    op.create_index(
        "ix_native_project_roots_project_id", "native_project_roots", ["project_id"]
    )
    op.create_table(
        "native_retained_bindings",
        _digest("key", primary_key=True),
        _digest("root_key"),
        _uuid("domain_id"),
        _json("payload"),
    )
    _domain_index("native_retained_bindings")
    op.create_index(
        "ix_native_retained_bindings_root_key", "native_retained_bindings", ["root_key"]
    )
    op.create_table(
        "native_storage_snapshots",
        _uuid("id", primary_key=True),
        _uuid("domain_id"),
        _uuid("enrollment_id"),
        _digest(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        _json("reason_codes"),
        _json("payload"),
    )
    _domain_index("native_storage_snapshots")


def downgrade() -> None:
    for name in (
        "native_storage_snapshots",
        "native_retained_bindings",
        "native_project_roots",
        "native_pool_claims",
        "native_collection_domains",
        "native_enrollments",
        "native_enrollment_requests",
        "native_reporting_targets",
    ):
        op.drop_table(name)
