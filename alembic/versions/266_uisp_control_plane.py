"""Add UISP desired/observed intents and config snapshots.

Revision ID: 266_uisp_control_plane
Revises: 265_uisp_subscription_ownership
"""

import sqlalchemy as sa

from alembic import op

revision = "266_uisp_control_plane"
down_revision = "265_uisp_subscription_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "uisp_device_intents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("target_type", sa.String(length=3), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=True),
        sa.Column("service_order_id", sa.UUID(), nullable=True),
        sa.Column("uisp_device_id", sa.String(length=120), nullable=True),
        sa.Column("desired_state", sa.JSON(), nullable=False),
        sa.Column("observed_config", sa.JSON(), nullable=True),
        sa.Column("drift", sa.JSON(), nullable=True),
        sa.Column("desired_revision", sa.Integer(), nullable=False),
        sa.Column("verified_revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["service_order_id"], ["service_orders.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_type", "target_id", name="uq_uisp_intent_target"),
    )
    op.create_index(
        "ix_uisp_intent_subscription",
        "uisp_device_intents",
        ["subscription_id"],
    )
    op.create_index(
        "ix_uisp_intent_service_order_id",
        "uisp_device_intents",
        ["service_order_id"],
    )
    op.create_index(
        "ix_uisp_intent_uisp_device_id",
        "uisp_device_intents",
        ["uisp_device_id"],
    )
    op.create_index("ix_uisp_intent_status", "uisp_device_intents", ["status"])

    op.create_table(
        "uisp_config_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("intent_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["uisp_device_intents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_uisp_snapshot_intent_created",
        "uisp_config_snapshots",
        ["intent_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_uisp_snapshot_intent_created", table_name="uisp_config_snapshots")
    op.drop_table("uisp_config_snapshots")
    op.drop_index("ix_uisp_intent_status", table_name="uisp_device_intents")
    op.drop_index("ix_uisp_intent_uisp_device_id", table_name="uisp_device_intents")
    op.drop_index("ix_uisp_intent_service_order_id", table_name="uisp_device_intents")
    op.drop_index("ix_uisp_intent_subscription", table_name="uisp_device_intents")
    op.drop_table("uisp_device_intents")
