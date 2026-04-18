"""Add dns threat events table.

Revision ID: u7v8w9x0y1z2
Revises: f2a3b4c5d6e7, r2s3t4u5v6w7
Create Date: 2026-03-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "u7v8w9x0y1z2"
down_revision = ("f2a3b4c5d6e7", "r2s3t4u5v6w7")
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    dns_threat_severity = postgresql.ENUM(
        "low",
        "medium",
        "high",
        "critical",
        name="dnsthreatseverity",
        create_type=False,
    )
    dns_threat_action = postgresql.ENUM(
        "blocked",
        "allowed",
        "monitored",
        name="dnsthreataction",
        create_type=False,
    )
    dns_threat_severity.create(bind, checkfirst=True)
    dns_threat_action.create(bind, checkfirst=True)

    if not inspector.has_table("dns_threat_events"):
        op.create_table(
            "dns_threat_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "network_device_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
            sa.Column("pop_site_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("queried_domain", sa.String(length=255), nullable=False),
            sa.Column("query_type", sa.String(length=16), nullable=True),
            sa.Column("source_ip", sa.String(length=64), nullable=True),
            sa.Column("destination_ip", sa.String(length=64), nullable=True),
            sa.Column("threat_category", sa.String(length=80), nullable=True),
            sa.Column("threat_feed", sa.String(length=120), nullable=True),
            sa.Column(
                "severity", dns_threat_severity, nullable=False, server_default="medium"
            ),
            sa.Column(
                "action", dns_threat_action, nullable=False, server_default="blocked"
            ),
            sa.Column("confidence_score", sa.Float(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
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
            sa.ForeignKeyConstraint(["network_device_id"], ["network_devices.id"]),
            sa.ForeignKeyConstraint(["pop_site_id"], ["pop_sites.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_dns_threat_events_subscriber_id",
            "dns_threat_events",
            ["subscriber_id"],
        )
        op.create_index(
            "ix_dns_threat_events_network_device_id",
            "dns_threat_events",
            ["network_device_id"],
        )
        op.create_index(
            "ix_dns_threat_events_pop_site_id",
            "dns_threat_events",
            ["pop_site_id"],
        )
        op.create_index(
            "ix_dns_threat_events_occurred_at",
            "dns_threat_events",
            ["occurred_at"],
        )
        op.create_index(
            "ix_dns_threat_events_severity",
            "dns_threat_events",
            ["severity"],
        )
        op.create_index(
            "ix_dns_threat_events_action",
            "dns_threat_events",
            ["action"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("dns_threat_events"):
        op.drop_index("ix_dns_threat_events_action", table_name="dns_threat_events")
        op.drop_index("ix_dns_threat_events_severity", table_name="dns_threat_events")
        op.drop_index(
            "ix_dns_threat_events_occurred_at", table_name="dns_threat_events"
        )
        op.drop_index(
            "ix_dns_threat_events_pop_site_id",
            table_name="dns_threat_events",
        )
        op.drop_index(
            "ix_dns_threat_events_network_device_id",
            table_name="dns_threat_events",
        )
        op.drop_index(
            "ix_dns_threat_events_subscriber_id",
            table_name="dns_threat_events",
        )
        op.drop_table("dns_threat_events")

    postgresql.ENUM(name="dnsthreataction").drop(bind, checkfirst=True)
    postgresql.ENUM(name="dnsthreatseverity").drop(bind, checkfirst=True)
