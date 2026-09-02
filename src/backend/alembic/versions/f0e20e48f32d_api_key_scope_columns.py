"""api_key scope columns

Revision ID: f0e20e48f32d
Revises: 43bd1cef2343
Create Date: 2026-09-02 07:25:10.981120

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0e20e48f32d"
down_revision: str | Sequence[str] | None = "43bd1cef2343"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "api_keys",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_api_keys_project_id"), "api_keys", ["project_id"], unique=False
    )
    op.add_column(
        "api_keys",
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("api_keys", "scopes")
    op.drop_index(op.f("ix_api_keys_project_id"), table_name="api_keys")
    op.drop_column("api_keys", "project_id")
