"""New groups are authoritative by default; no live membership conversion.

Revision ID: 8fbde773a642
Revises: 7eacbdd6fe04
"""
import sqlalchemy as sa

from alembic import op

revision = "8fbde773a642"
down_revision = "7eacbdd6fe04"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("managed_groups", "is_organization", server_default=sa.true())


def downgrade():
    op.alter_column("managed_groups", "is_organization", server_default=sa.false())
