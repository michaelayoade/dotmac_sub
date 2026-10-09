"""Bounded subscription test-access grants and existing-role permission grants."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "645_subscription_test_connection"
down_revision = "644_automation_scheduled_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_connection_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("actor_label", sa.String(160), nullable=False),
        sa.Column(
            "command_id", postgresql.UUID(as_uuid=True), unique=True, nullable=False
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_state", sa.String(16), nullable=False),
        sa.Column("delivery_error", sa.String(160)),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("duration_seconds > 0", name="ck_test_connection_duration"),
        sa.CheckConstraint(
            "expires_at > activated_at", name="ck_test_connection_interval"
        ),
        sa.CheckConstraint(
            "delivery_state IN ('pending', 'applied', 'failed')",
            name="ck_test_connection_delivery",
        ),
    )
    op.create_index(
        "uq_test_connection_open_subscription",
        "test_connection_grants",
        ["subscription_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_test_connection_account_time",
        "test_connection_grants",
        ["subscriber_id", "activated_at"],
    )
    op.create_index(
        "ix_test_connection_expiry", "test_connection_grants", ["expires_at"]
    )
    # Additive catalog convergence preserves every existing/custom role grant.
    op.execute(
        sa.text("""
        INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at)
        VALUES (gen_random_uuid(), 'subscription:test_connection', 'Temporarily enable subscription connectivity testing', true, true, now(), now())
        ON CONFLICT (key) DO NOTHING
    """)
    )
    op.execute(
        sa.text("""
        INSERT INTO role_permissions (id, role_id, permission_id)
        SELECT gen_random_uuid(), roles.id, permissions.id FROM roles CROSS JOIN permissions
        WHERE roles.name IN ('customer_experience_manager', 'finance_manager', 'admin')
          AND permissions.key = 'subscription:test_connection'
        ON CONFLICT (role_id, permission_id) DO NOTHING
    """)
    )


def downgrade() -> None:
    # RBAC grants can have acquired additional assignments; retain the catalog
    # rather than destructively deleting an operator's subsequent policy.
    op.drop_table("test_connection_grants")
