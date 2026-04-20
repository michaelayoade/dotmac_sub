"""Add SNMP configuration columns to OLT devices.

Revision ID: 042_add_olt_snmp_config
Revises: 041_widen_encrypted_credential_columns
Create Date: 2026-04-19

Adds configurable SNMP settings per OLT:
- snmp_timeout_seconds: Custom timeout for slow OLTs (default: 45)
- snmp_bulk_enabled: Whether to use snmpbulkwalk (default: True)
- snmp_bulk_max_repetitions: GetBulk max-repetitions (default: 50)
- poll_cycle_number: Current tiered polling cycle (for persistent state)
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "042_add_olt_snmp_config"
down_revision = "041_widen_encrypted_credential_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if columns already exist (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    if "snmp_timeout_seconds" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "snmp_timeout_seconds",
                sa.Integer(),
                nullable=True,
                comment="Custom SNMP timeout in seconds (default: 45)",
            ),
        )

    if "snmp_bulk_enabled" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "snmp_bulk_enabled",
                sa.Boolean(),
                nullable=True,
                server_default=sa.text("true"),
                comment="Use snmpbulkwalk for faster table walks",
            ),
        )

    if "snmp_bulk_max_repetitions" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "snmp_bulk_max_repetitions",
                sa.Integer(),
                nullable=True,
                comment="GetBulk max-repetitions (default: 50)",
            ),
        )

    if "poll_cycle_number" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "poll_cycle_number",
                sa.Integer(),
                nullable=True,
                server_default=sa.text("0"),
                comment="Current tiered polling cycle number",
            ),
        )


def downgrade() -> None:
    # Remove columns if they exist
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    if "poll_cycle_number" in existing_columns:
        op.drop_column("olt_devices", "poll_cycle_number")

    if "snmp_bulk_max_repetitions" in existing_columns:
        op.drop_column("olt_devices", "snmp_bulk_max_repetitions")

    if "snmp_bulk_enabled" in existing_columns:
        op.drop_column("olt_devices", "snmp_bulk_enabled")

    if "snmp_timeout_seconds" in existing_columns:
        op.drop_column("olt_devices", "snmp_timeout_seconds")
