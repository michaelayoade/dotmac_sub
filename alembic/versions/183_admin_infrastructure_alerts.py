"""Add admin infrastructure alerts and inbox notifications.

Revision ID: 183_admin_infrastructure_alerts
Revises: 182_rbac_permission_visibility
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "183_admin_infrastructure_alerts"
down_revision = "182_rbac_permission_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "admin_alerts" not in inspector.get_table_names():
        op.create_table(
            "admin_alerts",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("category", sa.String(length=40), nullable=False),
            sa.Column("source", sa.String(length=80), nullable=False),
            sa.Column("fingerprint", sa.String(length=180), nullable=False),
            sa.Column(
                "severity",
                sa.Enum(
                    "info",
                    "warning",
                    "critical",
                    name="alertseverity",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.Enum(
                    "open",
                    "acknowledged",
                    "resolved",
                    name="alertstatus",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column("summary", sa.String(length=255), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("target_url", sa.String(length=255), nullable=True),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("fingerprint", name="uq_admin_alerts_fingerprint"),
        )
        op.create_index(
            "ix_admin_alerts_category",
            "admin_alerts",
            ["category"],
            unique=False,
        )
        op.create_index(
            "ix_admin_alerts_category_status",
            "admin_alerts",
            ["category", "status"],
            unique=False,
        )
        op.create_index(
            "ix_admin_alerts_source",
            "admin_alerts",
            ["source"],
            unique=False,
        )

    if "admin_notifications" not in inspector.get_table_names():
        op.create_table(
            "admin_notifications",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("alert_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("target_url", sa.String(length=255), nullable=False),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["alert_id"], ["admin_alerts.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["system_user_id"], ["system_users.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "alert_id",
                "system_user_id",
                name="uq_admin_notifications_alert_user",
            ),
        )
        op.create_index(
            "ix_admin_notifications_user_read",
            "admin_notifications",
            ["system_user_id", "read_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "admin_notifications" in inspector.get_table_names():
        op.drop_index(
            "ix_admin_notifications_user_read",
            table_name="admin_notifications",
        )
        op.drop_table("admin_notifications")
    if "admin_alerts" in inspector.get_table_names():
        op.drop_index("ix_admin_alerts_source", table_name="admin_alerts")
        op.drop_index(
            "ix_admin_alerts_category_status",
            table_name="admin_alerts",
        )
        op.drop_index("ix_admin_alerts_category", table_name="admin_alerts")
        op.drop_table("admin_alerts")
