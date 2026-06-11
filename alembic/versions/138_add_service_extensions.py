"""Add service extensions (outage validity compensation).

Revision ID: 138_add_service_extensions
Revises: 137_extend_user_invite_expiry
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSON, UUID

from alembic import op

revision = "138_add_service_extensions"
down_revision = "137_extend_user_invite_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()

    if "service_extensions" not in tables:
        op.create_table(
            "service_extensions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("days", sa.Integer(), nullable=False),
            sa.Column(
                "scope_type",
                sa.Enum(
                    "network",
                    "pop_site",
                    "nas_device",
                    "subscribers",
                    name="serviceextensionscope",
                ),
                nullable=False,
            ),
            sa.Column("scope_id", UUID(as_uuid=True), nullable=True),
            sa.Column("scope_subscriber_ids", JSON(), nullable=True),
            sa.Column(
                "status",
                sa.Enum(
                    "pending",
                    "applied",
                    "canceled",
                    name="serviceextensionstatus",
                ),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "affected_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "skipped_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("applied_by", sa.String(64), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "service_extension_entries" not in tables:
        op.create_table(
            "service_extension_entries",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "extension_id",
                UUID(as_uuid=True),
                sa.ForeignKey("service_extensions.id"),
                nullable=False,
            ),
            sa.Column(
                "subscription_id",
                UUID(as_uuid=True),
                sa.ForeignKey("subscriptions.id"),
                nullable=False,
            ),
            sa.Column(
                "subscriber_id",
                UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id"),
                nullable=False,
            ),
            sa.Column(
                "previous_next_billing_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "new_next_billing_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_service_extension_entries_extension",
            "service_extension_entries",
            ["extension_id"],
        )
        op.create_index(
            "ix_service_extension_entries_subscription",
            "service_extension_entries",
            ["subscription_id"],
        )


def downgrade() -> None:
    op.drop_table("service_extension_entries")
    op.drop_table("service_extensions")
    sa.Enum(name="serviceextensionscope").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="serviceextensionstatus").drop(op.get_bind(), checkfirst=True)
