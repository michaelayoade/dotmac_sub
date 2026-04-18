"""Add OLT polling health tracking fields.

Adds fields to track OLT reachability/polling status:
- last_poll_at: When the OLT was last polled
- last_poll_status: success/failed/timeout
- last_poll_error: Error message if poll failed
- consecutive_poll_failures: Counter for alerting thresholds

Revision ID: 003_add_olt_polling_health_fields
Revises: 002_drop_subscription_id_from_devices
Create Date: 2026-04-01

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "003_add_olt_polling_health_fields"
down_revision = "002_drop_subscription_id_from_devices"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _column_exists(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    # Create the pollstatus enum if it doesn't exist
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'pollstatus') THEN
                CREATE TYPE pollstatus AS ENUM ('success', 'failed', 'timeout');
            END IF;
        END
        $$;
    """)

    # Add polling health fields to olt_devices
    _add_column_if_missing(
        "olt_devices",
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "olt_devices",
        sa.Column(
            "last_poll_status",
            sa.Enum(
                "success",
                "failed",
                "timeout",
                name="pollstatus",
                create_constraint=False,
            ),
            nullable=True,
        ),
    )
    _add_column_if_missing(
        "olt_devices",
        sa.Column("last_poll_error", sa.String(500), nullable=True),
    )
    _add_column_if_missing(
        "olt_devices",
        sa.Column(
            "consecutive_poll_failures", sa.Integer(), nullable=True, server_default="0"
        ),
    )

    # Set default for existing rows
    op.execute(
        "UPDATE olt_devices SET consecutive_poll_failures = 0 WHERE consecutive_poll_failures IS NULL"
    )


def downgrade() -> None:
    _drop_column_if_exists("olt_devices", "consecutive_poll_failures")
    _drop_column_if_exists("olt_devices", "last_poll_error")
    _drop_column_if_exists("olt_devices", "last_poll_status")
    _drop_column_if_exists("olt_devices", "last_poll_at")

    # Note: We don't drop the pollstatus enum in downgrade as it may be used elsewhere
