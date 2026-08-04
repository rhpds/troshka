"""add network mesh

Revision ID: 67320038e4ea
Revises: e66ef9d238cf
Create Date: 2026-08-04 13:27:41.616137

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "67320038e4ea"
down_revision: str | Sequence[str] | None = "e66ef9d238cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_mesh_peers",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "host_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("hosts.id"),
            nullable=True,
        ),
        sa.Column(
            "provider_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("providers.id"),
            nullable=True,
        ),
        sa.Column("peer_type", sa.String(20), nullable=False),
        sa.Column("wg_public_key", sa.String(64), nullable=False),
        sa.Column("wg_private_key", sa.String(256), nullable=False),
        sa.Column("wg_endpoint", sa.String(64), nullable=False),
        sa.Column("wg_address", sa.String(32), nullable=False),
        sa.Column("wg_port", sa.Integer, nullable=False),
        sa.Column("is_network_host", sa.Boolean, default=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "projects",
        sa.Column("mesh_subnet_id", sa.Integer, nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "mesh_network_host_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("hosts.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "host_assignments",
            sa.dialects.postgresql.JSONB,
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "host_assignments")
    op.drop_column("projects", "mesh_network_host_id")
    op.drop_column("projects", "mesh_subnet_id")
    op.drop_table("project_mesh_peers")
