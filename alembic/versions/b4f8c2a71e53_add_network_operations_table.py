"""Add network_operations table

Revision ID: b4f8c2a71e53
Revises: a2de92d25263
Create Date: 2026-03-23

"""

from sqlalchemy import inspect, text

from alembic import op

revision = "b4f8c2a71e53"
down_revision = "a2de92d25263"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    # Create enum types (idempotent)
    conn.execute(
        text(
            "DO $$ BEGIN "
            "CREATE TYPE networkoperationstatus AS ENUM "
            "('pending', 'running', 'waiting', 'succeeded', 'failed', 'canceled'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )
    conn.execute(
        text(
            "DO $$ BEGIN "
            "CREATE TYPE networkoperationtype AS ENUM ("
            "'olt_ont_sync', 'ont_provision', 'ont_authorize', "
            "'ont_reboot', 'ont_factory_reset', 'ont_set_pppoe', "
            "'ont_set_conn_request_creds', 'ont_send_conn_request', "
            "'ont_enable_ipv6', "
            "'cpe_set_conn_request_creds', 'cpe_send_conn_request', "
            "'cpe_reboot', 'cpe_factory_reset', "
            "'tr069_bootstrap', 'wifi_update', 'pppoe_push'"
            "); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )
    conn.execute(
        text(
            "DO $$ BEGIN "
            "CREATE TYPE networkoperationtargettype AS ENUM "
            "('olt', 'ont', 'cpe'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )

    if not inspector.has_table("network_operations"):
        conn.execute(text("""
            CREATE TABLE network_operations (
                id UUID PRIMARY KEY,
                operation_type networkoperationtype NOT NULL,
                target_type networkoperationtargettype NOT NULL,
                target_id UUID NOT NULL,
                parent_id UUID REFERENCES network_operations(id) ON DELETE CASCADE,
                status networkoperationstatus NOT NULL DEFAULT 'pending',
                correlation_key VARCHAR(255),
                waiting_reason TEXT,
                input_payload JSON,
                output_payload JSON,
                error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                initiated_by VARCHAR(120),
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))

        op.create_index(
            "ix_netops_target",
            "network_operations",
            ["target_type", "target_id"],
        )
        op.create_index(
            "ix_netops_status",
            "network_operations",
            ["status"],
        )
        op.create_index(
            "ix_netops_parent",
            "network_operations",
            ["parent_id"],
        )
        # Partial unique index: one active operation per correlation key
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_netops_active_correlation "
            "ON network_operations (correlation_key) "
            "WHERE status IN ('pending', 'running', 'waiting')"
        ))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    if inspector.has_table("network_operations"):
        op.drop_table("network_operations")
    conn.execute(text("DROP TYPE IF EXISTS networkoperationstatus"))
    conn.execute(text("DROP TYPE IF EXISTS networkoperationtype"))
    conn.execute(text("DROP TYPE IF EXISTS networkoperationtargettype"))
