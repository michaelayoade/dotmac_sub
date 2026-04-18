"""Add RADIUS error tracking, portal messaging, invoice metadata, cached MRR.

Closes remaining Splynx gaps:
- radius_auth_errors: RADIUS auth failure tracking (Splynx error_session, 323K rows)
- portal_messages: subscriber-facing in-app messages (Splynx portal_messages)
- portal_onboarding_states: customer portal onboarding (Splynx portal_onboarding)
- invoices.metadata: JSONB column for extensible invoice data
- subscribers.mrr_total: cached MRR for quick lookups
- invoice_lines.metadata: upgrade Text → JSONB
- credit_note_lines.metadata: upgrade Text → JSONB

Revision ID: o3p4q5r6s7t8
Revises: n2o3p4q5r6s7
Create Date: 2026-03-15 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "o3p4q5r6s7t8"
down_revision = "n2o3p4q5r6s7"
branch_labels = None
depends_on = None


def _coerce_text_column_to_jsonb(table_name: str, inspector: sa.Inspector) -> None:
    columns = {c["name"]: c for c in inspector.get_columns(table_name)}
    metadata_col = columns.get("metadata")
    if metadata_col is None:
        return

    col_type = metadata_col["type"]
    if isinstance(col_type, sa.Text):
        op.execute(
            """
            CREATE OR REPLACE FUNCTION _safe_text_to_jsonb(value text)
            RETURNS jsonb
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF value IS NULL THEN
                    RETURN NULL;
                END IF;

                BEGIN
                    RETURN value::jsonb;
                EXCEPTION
                    WHEN others THEN
                        RETURN to_jsonb(value);
                END;
            END;
            $$;
            """
        )
        try:
            op.execute(
                sa.text(
                    f"ALTER TABLE {table_name} "
                    "ALTER COLUMN metadata TYPE jsonb "
                    "USING _safe_text_to_jsonb(metadata)"
                )
            )
        finally:
            op.execute("DROP FUNCTION IF EXISTS _safe_text_to_jsonb(text)")


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # --- Enum types ---
    for enum_name, values in [
        (
            "radiusautherrortype",
            [
                "reject",
                "timeout",
                "invalid_credentials",
                "disabled_account",
                "expired_account",
                "nas_mismatch",
                "policy_violation",
                "other",
            ],
        ),
        (
            "portalmessagetype",
            [
                "welcome",
                "announcement",
                "billing",
                "service",
                "support",
                "system",
            ],
        ),
        ("portalmessagestatus", ["unread", "read", "archived"]),
    ]:
        enum = postgresql.ENUM(*values, name=enum_name, create_type=False)
        enum.create(conn, checkfirst=True)

    # --- 1. radius_auth_errors ---
    if "radius_auth_errors" not in existing_tables:
        op.create_table(
            "radius_auth_errors",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
                "nas_device_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("nas_devices.id"),
                nullable=True,
            ),
            sa.Column("username", sa.String(120), nullable=False),
            sa.Column("nas_ip_address", sa.String(64), nullable=True),
            sa.Column("calling_station_id", sa.String(64), nullable=True),
            sa.Column(
                "error_type",
                postgresql.ENUM(
                    "reject",
                    "timeout",
                    "invalid_credentials",
                    "disabled_account",
                    "expired_account",
                    "nas_mismatch",
                    "policy_violation",
                    "other",
                    name="radiusautherrortype",
                    create_type=False,
                ),
                nullable=False,
                server_default="reject",
            ),
            sa.Column("reply_message", sa.String(255), nullable=True),
            sa.Column("detail", sa.Text, nullable=True),
            sa.Column(
                "occurred_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_radius_auth_errors_username",
            "radius_auth_errors",
            ["username"],
        )
        op.create_index(
            "ix_radius_auth_errors_occurred_at",
            "radius_auth_errors",
            ["occurred_at"],
        )
        op.create_index(
            "ix_radius_auth_errors_nas",
            "radius_auth_errors",
            ["nas_device_id"],
        )
        op.create_index(
            "ix_radius_auth_errors_subscriber",
            "radius_auth_errors",
            ["subscriber_id"],
        )

    # --- 2. portal_messages ---
    if "portal_messages" not in existing_tables:
        op.create_table(
            "portal_messages",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "message_type",
                postgresql.ENUM(
                    "welcome",
                    "announcement",
                    "billing",
                    "service",
                    "support",
                    "system",
                    name="portalmessagetype",
                    create_type=False,
                ),
                nullable=False,
                server_default="system",
            ),
            sa.Column("subject", sa.String(255), nullable=False),
            sa.Column("body", sa.Text, nullable=False),
            sa.Column(
                "status",
                postgresql.ENUM(
                    "unread",
                    "read",
                    "archived",
                    name="portalmessagestatus",
                    create_type=False,
                ),
                nullable=False,
                server_default="unread",
            ),
            sa.Column(
                "is_pinned",
                sa.Boolean,
                server_default=sa.text("false"),
            ),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_portal_messages_subscriber",
            "portal_messages",
            ["subscriber_id"],
        )
        op.create_index(
            "ix_portal_messages_status",
            "portal_messages",
            ["status"],
        )

    # --- 3. portal_onboarding_states ---
    if "portal_onboarding_states" not in existing_tables:
        op.create_table(
            "portal_onboarding_states",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "steps_completed",
                sa.Integer,
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "is_complete",
                sa.Boolean,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "completed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
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
        )

    # --- 4. Add metadata JSONB column to invoices ---
    inv_cols = {c["name"] for c in inspector.get_columns("invoices")}
    if "metadata" not in inv_cols:
        op.add_column(
            "invoices",
            sa.Column("metadata", postgresql.JSONB, nullable=True),
        )

    # --- 5. Add mrr_total to subscribers ---
    sub_cols = {c["name"] for c in inspector.get_columns("subscribers")}
    if "mrr_total" not in sub_cols:
        op.add_column(
            "subscribers",
            sa.Column(
                "mrr_total",
                sa.Numeric(12, 2),
                nullable=True,
                server_default="0",
            ),
        )

    # --- 6. Upgrade invoice_lines.metadata from Text to JSONB ---
    # PostgreSQL ALTER COLUMN TYPE with USING cast
    _coerce_text_column_to_jsonb("invoice_lines", inspector)

    # --- 7. Upgrade credit_note_lines.metadata from Text to JSONB ---
    _coerce_text_column_to_jsonb("credit_note_lines", inspector)


def downgrade() -> None:
    # Revert metadata columns to Text
    op.execute(
        "ALTER TABLE credit_note_lines "
        "ALTER COLUMN metadata TYPE text "
        "USING metadata::text"
    )
    op.execute(
        "ALTER TABLE invoice_lines ALTER COLUMN metadata TYPE text USING metadata::text"
    )

    op.drop_column("subscribers", "mrr_total")
    op.drop_column("invoices", "metadata")

    op.drop_table("portal_onboarding_states")
    op.drop_table("portal_messages")
    op.drop_table("radius_auth_errors")

    for enum_name in [
        "portalmessagestatus",
        "portalmessagetype",
        "radiusautherrortype",
    ]:
        postgresql.ENUM(name=enum_name).drop(op.get_bind(), checkfirst=True)
