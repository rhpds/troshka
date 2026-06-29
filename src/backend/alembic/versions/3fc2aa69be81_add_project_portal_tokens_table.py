"""add project_portal_tokens table

Revision ID: 3fc2aa69be81
Revises: d8f5d6d8dce9
Create Date: 2026-06-14 15:25:02.215264

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3fc2aa69be81"
down_revision: str | Sequence[str] | None = "d8f5d6d8dce9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_portal_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column(
            "access_level", sa.String(20), nullable=False, server_default="readonly"
        ),
        sa.Column("expires_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("project_portal_tokens")
