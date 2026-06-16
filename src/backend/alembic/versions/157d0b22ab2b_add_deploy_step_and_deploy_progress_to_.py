"""add deploy_step and deploy_progress to projects

Revision ID: 157d0b22ab2b
Revises: 88043a868aa7
Create Date: 2026-06-16 12:46:51.770898

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = "157d0b22ab2b"
down_revision: Union[str, Sequence[str], None] = "88043a868aa7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("deploy_step", sa.String(30), nullable=True))
    op.add_column(
        "projects",
        sa.Column("deploy_progress", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "deploy_progress")
    op.drop_column("projects", "deploy_step")
