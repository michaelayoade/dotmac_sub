"""add_speed_test_results_table

Revision ID: r8s9t0u1v2w3
Revises: c3d4e5f6a7b8, m9n8o7p6q5r4, p0q1r2s3t4u5
Create Date: 2026-03-08 10:05:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "r8s9t0u1v2w3"
down_revision = ("c3d4e5f6a7b8", "m9n8o7p6q5r4", "p0q1r2s3t4u5")
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    speedtestsource = postgresql.ENUM(
        "manual",
        "scheduled",
        "api",
        name="speedtestsource",
        create_type=False,
    )
    speedtestsource.create(bind, checkfirst=True)

    if not inspector.has_table("speed_test_results"):
        op.create_table(
            "speed_test_results",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "network_device_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
            sa.Column("pop_site_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "source", speedtestsource, nullable=False, server_default="manual"
            ),
            sa.Column("target_label", sa.String(length=160), nullable=True),
            sa.Column("provider", sa.String(length=120), nullable=True),
            sa.Column("server_name", sa.String(length=160), nullable=True),
            sa.Column("external_ip", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=500), nullable=True),
            sa.Column("download_mbps", sa.Float(), nullable=False, server_default="0"),
            sa.Column("upload_mbps", sa.Float(), nullable=False, server_default="0"),
            sa.Column("latency_ms", sa.Float(), nullable=True),
            sa.Column("jitter_ms", sa.Float(), nullable=True),
            sa.Column("packet_loss_pct", sa.Float(), nullable=True),
            sa.Column("tested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"]),
            sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
            sa.ForeignKeyConstraint(["network_device_id"], ["network_devices.id"]),
            sa.ForeignKeyConstraint(["pop_site_id"], ["pop_sites.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_speed_test_results_subscriber_id",
            "speed_test_results",
            ["subscriber_id"],
        )
        op.create_index(
            "ix_speed_test_results_subscription_id",
            "speed_test_results",
            ["subscription_id"],
        )
        op.create_index(
            "ix_speed_test_results_network_device_id",
            "speed_test_results",
            ["network_device_id"],
        )
        op.create_index(
            "ix_speed_test_results_pop_site_id", "speed_test_results", ["pop_site_id"]
        )
        op.create_index(
            "ix_speed_test_results_tested_at", "speed_test_results", ["tested_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("speed_test_results"):
        op.drop_index(
            "ix_speed_test_results_tested_at", table_name="speed_test_results"
        )
        op.drop_index(
            "ix_speed_test_results_pop_site_id", table_name="speed_test_results"
        )
        op.drop_index(
            "ix_speed_test_results_network_device_id", table_name="speed_test_results"
        )
        op.drop_index(
            "ix_speed_test_results_subscription_id", table_name="speed_test_results"
        )
        op.drop_index(
            "ix_speed_test_results_subscriber_id", table_name="speed_test_results"
        )
        op.drop_table("speed_test_results")

    speedtestsource = postgresql.ENUM(name="speedtestsource")
    speedtestsource.drop(bind, checkfirst=True)
