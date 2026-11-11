"""Organization membership and durable quota foundation.

Revision ID: f62c48d91a07
Revises: e47c2a9b6103
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f62c48d91a07"
down_revision = "e47c2a9b6103"
branch_labels = None
depends_on = None

# Fixed migration identity; never inferred from existing reporting memberships.
DEFAULT_ORGANIZATION_ID = "dc591874-9d97-4bf5-ab31-581bb95cc645"


def upgrade() -> None:
    op.add_column(
        "managed_groups",
        sa.Column(
            "is_organization", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "managed_groups", sa.Column("cost_center_id", sa.String(255), nullable=True)
    )
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("organization_id", postgresql.UUID(as_uuid=False), nullable=True)
        )
        batch.create_foreign_key(
            "fk_users_organization_id",
            "managed_groups",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_users_organization_id", ["organization_id"])
    op.create_table(
        "quota_policies",
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("cpu", sa.BigInteger(), nullable=True),
        sa.Column("ram_bytes", sa.BigInteger(), nullable=True),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("projects", sa.BigInteger(), nullable=True),
        sa.Column("spending_usd", sa.Numeric(precision=38, scale=12), nullable=True),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('organization', 'user', 'project')", name="ck_quota_policy_scope"
        ),
        sa.CheckConstraint("cpu IS NULL OR cpu >= 0", name="ck_quota_policy_cpu"),
        sa.CheckConstraint(
            "projects IS NULL OR projects >= 0", name="ck_quota_policy_projects"
        ),
        sa.CheckConstraint(
            "ram_bytes IS NULL OR ram_bytes >= 0", name="ck_quota_policy_ram_bytes"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_quota_policy_revision"),
        sa.CheckConstraint(
            "spending_usd IS NULL OR spending_usd >= 0",
            name="ck_quota_policy_spending_usd",
        ),
        sa.CheckConstraint(
            "spending_usd IS NULL OR spending_usd <= 99999999999999999999999999.999999999999",
            name="ck_quota_policy_finite_spending",
        ),
        sa.CheckConstraint(
            "storage_bytes IS NULL OR storage_bytes >= 0",
            name="ck_quota_policy_storage_bytes",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "scope_id", name="uq_quota_policy_scope"),
    )
    op.create_index(
        op.f("ix_quota_policies_scope_id"), "quota_policies", ["scope_id"], unique=False
    )
    op.create_table(
        "quota_reservations",
        sa.Column(
            "state", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("requested", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observation_id", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'dispatched', 'confirmed', 'released', 'failed', 'reconciling')",
            name="ck_quota_reservation_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
    )
    op.create_index(
        op.f("ix_quota_reservations_organization_id"),
        "quota_reservations",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_quota_reservations_owner_id"),
        "quota_reservations",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_quota_reservations_project_id"),
        "quota_reservations",
        ["project_id"],
        unique=False,
    )
    op.create_table(
        "quota_allocations",
        sa.Column(
            "state", sa.String(length=20), server_default="active", nullable=False
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("requested", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observation_id", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('active', 'stopped', 'released', 'reconciling')",
            name="ck_quota_allocation_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
    )
    op.create_index(
        op.f("ix_quota_allocations_organization_id"),
        "quota_allocations",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_quota_allocations_owner_id"),
        "quota_allocations",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_quota_allocations_project_id"),
        "quota_allocations",
        ["project_id"],
        unique=False,
    )
    op.create_table(
        "quota_charges",
        sa.Column("project_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("owner_name", sa.String(length=255), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("organization_name", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column("rate", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column(
            "provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount >= 0", name="ck_quota_charge_amount"),
        sa.CheckConstraint("end_at > start_at", name="ck_quota_charge_interval"),
        sa.CheckConstraint("quantity >= 0", name="ck_quota_charge_quantity"),
        sa.CheckConstraint("rate >= 0", name="ck_quota_charge_rate"),
        sa.CheckConstraint(
            "quantity <= 99999999999999999999999999.999999999999",
            name="ck_quota_charge_finite_quantity",
        ),
        sa.CheckConstraint(
            "rate <= 99999999999999999999999999.999999999999",
            name="ck_quota_charge_finite_rate",
        ),
        sa.CheckConstraint(
            "amount <= 99999999999999999999999999.999999999999",
            name="ck_quota_charge_finite_amount",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source", "source_id", "start_at", "end_at", name="uq_quota_charge_segment"
        ),
    )
    op.create_index(
        op.f("ix_quota_charges_organization_id"),
        "quota_charges",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_quota_charges_owner_id"), "quota_charges", ["owner_id"], unique=False
    )
    op.create_index(
        op.f("ix_quota_charges_project_id"),
        "quota_charges",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_quota_charges_start_at"), "quota_charges", ["start_at"], unique=False
    )
    op.create_table(
        "quota_audits",
        sa.Column("actor_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "effective_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_quota_audits_actor_id"), "quota_audits", ["actor_id"], unique=False
    )
    op.create_index(
        op.f("ix_quota_audits_scope_id"), "quota_audits", ["scope_id"], unique=False
    )
    op.create_table(
        "quota_actions",
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("policy_revision", sa.BigInteger(), nullable=False),
        sa.Column("period_key", sa.String(length=40), nullable=False),
        sa.Column(
            "project_id", sa.String(length=36), server_default="", nullable=False
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column(
            "state", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("operation_id", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'issued', 'completed', 'failed', 'cancelled')",
            name="ck_quota_action_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_quota_action_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
        sa.UniqueConstraint(
            "scope",
            "scope_id",
            "policy_revision",
            "period_key",
            "kind",
            "threshold",
            "project_id",
            name="uq_quota_action_identity",
        ),
    )
    op.create_index(
        op.f("ix_quota_actions_scope_id"), "quota_actions", ["scope_id"], unique=False
    )
    op.create_index(
        op.f("ix_quota_actions_state"), "quota_actions", ["state"], unique=False
    )
    op.create_table(
        "quota_readiness",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("ready", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("baseline_version", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # This SQL is deliberately static so offline review renders the complete seed.
    # UUID string literals work on both PostgreSQL and SQLite migration tests.
    op.execute(
        sa.text(
            "INSERT INTO managed_groups (id, name, normalized_name, description, is_organization) VALUES ('dc591874-9d97-4bf5-ab31-581bb95cc645', 'Default organization', 'quota-default-dc591874-9d97-4bf5-ab31-581bb95cc645', 'Migration attribution; not evidence of historical employer', true)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE users SET organization_id = 'dc591874-9d97-4bf5-ab31-581bb95cc645' WHERE organization_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO quota_policies (id, scope, scope_id) VALUES ('cc4ecf45-0811-48a5-bca2-e440a3fbc55c', 'organization', 'dc591874-9d97-4bf5-ab31-581bb95cc645')"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO quota_readiness (id, ready, details) VALUES ('baseline', false, '{}')"
        )
    )


def downgrade() -> None:
    for table in (
        "quota_readiness",
        "quota_actions",
        "quota_audits",
        "quota_charges",
        "quota_allocations",
        "quota_reservations",
        "quota_policies",
    ):
        op.drop_table(table)
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_organization_id")
        batch.drop_constraint("fk_users_organization_id", type_="foreignkey")
        batch.drop_column("organization_id")
    op.execute(
        sa.text(
            "DELETE FROM managed_groups WHERE id = 'dc591874-9d97-4bf5-ab31-581bb95cc645'"
        )
    )
    with op.batch_alter_table("managed_groups") as batch:
        batch.drop_column("cost_center_id")
        batch.drop_column("is_organization")
