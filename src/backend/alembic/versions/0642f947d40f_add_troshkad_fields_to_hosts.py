"""add troshkad fields to hosts

Revision ID: 0642f947d40f
Revises: 068c16405f3e
Create Date: 2026-06-08 10:35:41.359847

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0642f947d40f"
down_revision: str | Sequence[str] | None = "068c16405f3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("hosts", sa.Column("agent_token", sa.Text(), nullable=True))
    op.add_column(
        "hosts", sa.Column("agent_cert_fingerprint", sa.String(100), nullable=True)
    )
    op.add_column("hosts", sa.Column("agent_version", sa.String(50), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("hosts", "agent_version")
    op.drop_column("hosts", "agent_cert_fingerprint")
    op.drop_column("hosts", "agent_token")
