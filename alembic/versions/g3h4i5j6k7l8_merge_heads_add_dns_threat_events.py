"""Merge heads and add DNS threat events storage.

Revision ID: g3h4i5j6k7l8
Revises: f2a3b4c5d6e7, r2s3t4u5v6w7
Create Date: 2026-03-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "g3h4i5j6k7l8"
down_revision: tuple[str, str] = ("f2a3b4c5d6e7", "r2s3t4u5v6w7")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    severity_enum = postgresql.ENUM(
        "low",
        "medium",
        "high",
        "critical",
        name="dnsthreatseverity",
    )
    action_enum = postgresql.ENUM(
        "blocked",
        "allowed",
        "monitored",
        name="dnsthreataction",
    )
    severity_enum.create(bind, checkfirst=True)
    action_enum.create(bind, checkfirst=True)

    if not inspector.has_table("dns_threat_events"):
        op.create_table(
            "dns_threat_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("network_device_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("pop_site_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("queried_domain", sa.String(length=255), nullable=False),
            sa.Column("query_type", sa.String(length=16), nullable=True),
            sa.Column("source_ip", sa.String(length=64), nullable=True),
            sa.Column("destination_ip", sa.String(length=64), nullable=True),
            sa.Column("threat_category", sa.String(length=80), nullable=True),
            sa.Column("threat_feed", sa.String(length=120), nullable=True),
            sa.Column("severity", severity_enum, nullable=False),
            sa.Column("action", action_enum, nullable=False),
            sa.Column("confidence_score", sa.Float(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["network_device_id"], ["network_devices.id"]),
            sa.ForeignKeyConstraint(["pop_site_id"], ["pop_sites.id"]),
            sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"]),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("dns_threat_events"):
        op.drop_table("dns_threat_events")

    action_enum = postgresql.ENUM(name="dnsthreataction")
    severity_enum = postgresql.ENUM(name="dnsthreatseverity")
    action_enum.drop(bind, checkfirst=True)
    severity_enum.drop(bind, checkfirst=True)
