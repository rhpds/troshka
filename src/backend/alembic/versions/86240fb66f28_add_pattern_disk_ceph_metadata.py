"""add_pattern_disk_ceph_metadata

Revision ID: 86240fb66f28
Revises: 33d3294c684b
Create Date: 2026-09-21 12:17:44.792597

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "86240fb66f28"
down_revision: str | Sequence[str] | None = "33d3294c684b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pattern_disks",
        sa.Column("source_kind", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "pattern_disks",
        sa.Column("source_index", sa.Integer(), nullable=True),
    )
    op.add_column(
        "pattern_disks",
        sa.Column("source_pvc_name", sa.String(length=253), nullable=True),
    )
    op.alter_column(
        "pattern_disks",
        "source_vm_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "pattern_disks",
        "source_vm_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.drop_column("pattern_disks", "source_pvc_name")
    op.drop_column("pattern_disks", "source_index")
    op.drop_column("pattern_disks", "source_kind")
