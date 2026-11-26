"""Add private assistant conversations, grants, evidence and audit metadata.

Revision ID: b7a260112601
Revises: a9f1c2d3e4b5
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b7a260112601"
down_revision = "a9f1c2d3e4b5"
branch_labels = None
depends_on = None


def _uuid(name, nullable=False, primary_key=False):
    return sa.Column(
        name, postgresql.UUID(as_uuid=False), nullable=nullable, primary_key=primary_key
    )


def _time(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


_INDEXES = {
    "assistant_conversations": ("owner_id", "project_id", "expires_at"),
    "assistant_grants": ("conversation_id",),
    "assistant_messages": ("conversation_id",),
    "assistant_evidence": ("conversation_id",),
    "assistant_audit_events": ("actor_id", "conversation_id", "created_at"),
}


def upgrade():
    op.create_table(
        "assistant_conversations",
        _uuid("id", primary_key=True),
        _uuid("owner_id"),
        _uuid("project_id", nullable=True),
        _time("created_at"),
        _time("expires_at"),
    )
    op.create_table(
        "assistant_grants",
        _uuid("id", primary_key=True),
        _uuid("conversation_id"),
        sa.Column("guest_checks", sa.Boolean(), nullable=False),
        _time("expires_at"),
        _time("revoked_at", nullable=True),
    )
    op.create_table(
        "assistant_messages",
        _uuid("id", primary_key=True),
        _uuid("conversation_id"),
        _uuid("request_id"),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        _uuid("grant_id", nullable=True),
        _time("created_at"),
        sa.UniqueConstraint("conversation_id", "request_id", "role"),
    )
    op.create_table(
        "assistant_evidence",
        _uuid("id", primary_key=True),
        _uuid("conversation_id"),
        _uuid("request_id"),
        _uuid("grant_id"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        _time("created_at"),
    )
    op.create_table(
        "assistant_audit_events",
        _uuid("id", primary_key=True),
        _uuid("actor_id"),
        _uuid("conversation_id"),
        sa.Column("check", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        _time("created_at"),
    )
    for table, columns in _INDEXES.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in reversed(_INDEXES):
        op.drop_table(table)
