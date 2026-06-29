"""rename default_ami to default_image

Revision ID: 99cc69011416
Revises: 8d54bbd22bdc
Create Date: 2026-06-16 16:15:28.771555

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "99cc69011416"
down_revision: str | Sequence[str] | None = "8d54bbd22bdc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("providers", "default_ami", new_column_name="default_image")


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("providers", "default_image", new_column_name="default_ami")
