"""Add FUP state, RADIUS active sessions, MRR snapshots, comms log, Splynx mapping.

Closes critical gaps identified in Splynx architecture comparison:
- fup_states: per-subscription FUP enforcement state (HIGH priority)
- radius_active_sessions: live RADIUS session tracking (MEDIUM)
- mrr_snapshots: historical MRR per subscriber (MEDIUM)
- communication_logs: migration target for Splynx mail/SMS pools (MEDIUM)
- splynx_id_mappings: generic integer↔UUID mapping for migration (MEDIUM)

Revision ID: m1n2o3p4q5r6
Revises: a8b9c0d1e2f3, f88cb663b8e0
Create Date: 2026-03-15 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "m1n2o3p4q5r6"
down_revision = (
    "a8b9c0d1e2f3",
    "f88cb663b8e0",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # --- Enum types (idempotent) ---
    for enum_name, values in [
        ("fupactionstatus", ["none", "throttled", "blocked", "notified"]),
        ("communicationdirection", ["inbound", "outbound"]),
        ("communicationchannel", ["email", "sms", "in_app", "whatsapp"]),
        (
            "communicationstatus",
            ["pending", "sent", "delivered", "failed", "bounced"],
        ),
        (
            "splynxentitytype",
            [
                "customer",
                "service",
                "tariff",
                "invoice",
                "payment",
                "transaction",
                "credit_note",
                "ticket",
                "quote",
                "router",
                "location",
                "partner",
                "email",
                "sms",
                "scheduling_task",
                "inventory_item",
            ],
        ),
    ]:
        enum = postgresql.ENUM(*values, name=enum_name, create_type=False)
        enum.create(conn, checkfirst=True)

    # --- 1. fup_states ---
    if "fup_states" not in existing_tables:
        op.create_table(
            "fup_states",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column(
                "subscription_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "offer_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("catalog_offers.id"),
                nullable=False,
            ),
            sa.Column(
                "active_rule_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("fup_rules.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "action_status",
                postgresql.ENUM(
                    "none",
                    "throttled",
                    "blocked",
                    "notified",
                    name="fupactionstatus",
                    create_type=False,
                ),
                nullable=False,
                server_default="none",
            ),
            sa.Column("speed_reduction_percent", sa.Float, nullable=True),
            sa.Column(
                "original_profile_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("radius_profiles.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "throttle_profile_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("radius_profiles.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "cap_resets_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "last_evaluated_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("subscription_id", name="uq_fup_states_subscription"),
        )

    # --- 2. radius_active_sessions ---
    if "radius_active_sessions" not in existing_tables:
        op.create_table(
            "radius_active_sessions",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id"),
                nullable=True,
            ),
            sa.Column(
                "subscription_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscriptions.id"),
                nullable=True,
            ),
            sa.Column(
                "access_credential_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("access_credentials.id"),
                nullable=True,
            ),
            sa.Column(
                "nas_device_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("nas_devices.id"),
                nullable=True,
            ),
            sa.Column("username", sa.String(120), nullable=False),
            sa.Column("acct_session_id", sa.String(120), nullable=False),
            sa.Column("nas_ip_address", sa.String(64), nullable=True),
            sa.Column("framed_ip_address", sa.String(64), nullable=True),
            sa.Column("framed_ipv6_prefix", sa.String(128), nullable=True),
            sa.Column("calling_station_id", sa.String(64), nullable=True),
            sa.Column("nas_port_id", sa.String(120), nullable=True),
            sa.Column(
                "session_start",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column("session_time", sa.Integer, server_default="0"),
            sa.Column("bytes_in", sa.BigInteger, server_default="0"),
            sa.Column("bytes_out", sa.BigInteger, server_default="0"),
            sa.Column("packets_in", sa.BigInteger, server_default="0"),
            sa.Column("packets_out", sa.BigInteger, server_default="0"),
            sa.Column(
                "last_update",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "acct_session_id",
                "nas_device_id",
                name="uq_radius_active_session",
            ),
        )
        op.create_index(
            "ix_radius_active_sessions_subscriber",
            "radius_active_sessions",
            ["subscriber_id"],
        )
        op.create_index(
            "ix_radius_active_sessions_subscription",
            "radius_active_sessions",
            ["subscription_id"],
        )
        op.create_index(
            "ix_radius_active_sessions_username",
            "radius_active_sessions",
            ["username"],
        )
        op.create_index(
            "ix_radius_active_sessions_nas",
            "radius_active_sessions",
            ["nas_device_id"],
        )

    # --- 3. mrr_snapshots ---
    if "mrr_snapshots" not in existing_tables:
        op.create_table(
            "mrr_snapshots",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("snapshot_date", sa.Date, nullable=False),
            sa.Column("mrr_amount", sa.Numeric(12, 2), nullable=False),
            sa.Column(
                "currency",
                sa.String(3),
                nullable=False,
                server_default="NGN",
            ),
            sa.Column(
                "active_subscriptions",
                sa.Integer,
                server_default="0",
            ),
            sa.Column("splynx_customer_id", sa.Integer, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "subscriber_id",
                "snapshot_date",
                name="uq_mrr_snapshot_subscriber_date",
            ),
        )
        op.create_index("ix_mrr_snapshots_date", "mrr_snapshots", ["snapshot_date"])
        op.create_index(
            "ix_mrr_snapshots_subscriber", "mrr_snapshots", ["subscriber_id"]
        )

    # --- 4. communication_logs ---
    if "communication_logs" not in existing_tables:
        op.create_table(
            "communication_logs",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id"),
                nullable=True,
            ),
            sa.Column(
                "subscription_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscriptions.id"),
                nullable=True,
            ),
            sa.Column(
                "channel",
                postgresql.ENUM(
                    "email",
                    "sms",
                    "in_app",
                    "whatsapp",
                    name="communicationchannel",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column(
                "direction",
                postgresql.ENUM(
                    "inbound",
                    "outbound",
                    name="communicationdirection",
                    create_type=False,
                ),
                nullable=False,
                server_default="outbound",
            ),
            sa.Column("recipient", sa.String(255), nullable=True),
            sa.Column("sender", sa.String(255), nullable=True),
            sa.Column("subject", sa.String(500), nullable=True),
            sa.Column("body", sa.Text, nullable=True),
            sa.Column(
                "status",
                postgresql.ENUM(
                    "pending",
                    "sent",
                    "delivered",
                    "failed",
                    "bounced",
                    name="communicationstatus",
                    create_type=False,
                ),
                nullable=False,
                server_default="sent",
            ),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("external_id", sa.String(200), nullable=True),
            sa.Column("splynx_message_id", sa.Integer, nullable=True),
            sa.Column("metadata", postgresql.JSON, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_communication_logs_subscriber",
            "communication_logs",
            ["subscriber_id"],
        )
        op.create_index(
            "ix_communication_logs_channel",
            "communication_logs",
            ["channel"],
        )
        op.create_index(
            "ix_communication_logs_sent_at",
            "communication_logs",
            ["sent_at"],
        )

    # --- 5. splynx_id_mappings ---
    if "splynx_id_mappings" not in existing_tables:
        op.create_table(
            "splynx_id_mappings",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column(
                "entity_type",
                postgresql.ENUM(
                    "customer",
                    "service",
                    "tariff",
                    "invoice",
                    "payment",
                    "transaction",
                    "credit_note",
                    "ticket",
                    "quote",
                    "router",
                    "location",
                    "partner",
                    "email",
                    "sms",
                    "scheduling_task",
                    "inventory_item",
                    name="splynxentitytype",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column("splynx_id", sa.Integer, nullable=False),
            sa.Column(
                "dotmac_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column(
                "migrated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("metadata", postgresql.JSON, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "entity_type",
                "splynx_id",
                name="uq_splynx_mapping_type_splynx_id",
            ),
            sa.UniqueConstraint(
                "entity_type",
                "dotmac_id",
                name="uq_splynx_mapping_type_dotmac_id",
            ),
        )


def downgrade() -> None:
    op.drop_table("splynx_id_mappings")
    op.drop_table("communication_logs")
    op.drop_table("mrr_snapshots")
    op.drop_table("radius_active_sessions")
    op.drop_table("fup_states")

    for enum_name in [
        "splynxentitytype",
        "communicationstatus",
        "communicationchannel",
        "communicationdirection",
        "fupactionstatus",
    ]:
        postgresql.ENUM(name=enum_name).drop(op.get_bind(), checkfirst=True)
