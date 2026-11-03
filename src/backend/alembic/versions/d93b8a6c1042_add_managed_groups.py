"""Add independent managed billing groups and assignments."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d93b8a6c1042"
down_revision = "c82589e4261c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_groups",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    for table in ("managed_group_members", "managed_group_managers"):
        op.create_table(
            table,
            sa.Column(
                "group_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("managed_groups.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
        )
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])


def downgrade() -> None:
    for table in ("managed_group_managers", "managed_group_members"):
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_table(table)
    op.drop_table("managed_groups")
