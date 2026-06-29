"""add elastic_ips table and host max_eips

Revision ID: 068c16405f3e
Revises: fa4247629ec7
Create Date: 2026-06-07 16:25:00.976147

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "068c16405f3e"
down_revision: str | Sequence[str] | None = "fa4247629ec7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add max_eips column to hosts table
    op.add_column(
        "hosts", sa.Column("max_eips", sa.Integer(), nullable=False, server_default="0")
    )

    # Create elastic_ips table
    op.create_table(
        "elastic_ips",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("canvas_eip_id", sa.String(length=100), nullable=False),
        sa.Column("allocation_id", sa.String(length=100), nullable=False),
        sa.Column("public_ip", sa.String(length=45), nullable=False),
        sa.Column("private_ip", sa.String(length=45), nullable=True),
        sa.Column("host_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("association_id", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop elastic_ips table
    op.drop_table("elastic_ips")

    # Drop max_eips column from hosts table
    op.drop_column("hosts", "max_eips")
