"""Add OLT capability flags for firmware-specific command support.

Revision ID: add_olt_capability_flags
Revises: add_onu_type_tr069_paths
Create Date: 2026-05-06

Adds:
- supports_ont_internet_config: False for MA5608T V800R013
- supports_ont_wan_config: False for MA5608T V800R013
"""

import sqlalchemy as sa

from alembic import op

revision = "add_olt_capability_flags"
down_revision = "add_onu_type_tr069_paths"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    # Add supports_ont_internet_config flag
    if not _column_exists("olt_devices", "supports_ont_internet_config"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "supports_ont_internet_config",
                sa.Boolean(),
                nullable=False,
                server_default="true",
            ),
        )

    # Add supports_ont_wan_config flag
    if not _column_exists("olt_devices", "supports_ont_wan_config"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "supports_ont_wan_config",
                sa.Boolean(),
                nullable=False,
                server_default="true",
            ),
        )

    # Set known MA5608T V800R013 OLTs to False
    # This is a data migration - update OLTs matching the pattern
    op.execute(
        """
        UPDATE olt_devices
        SET supports_ont_internet_config = false,
            supports_ont_wan_config = false
        WHERE model ILIKE '%MA5608T%'
          AND firmware_version ILIKE '%V800R013%'
        """
    )


def downgrade() -> None:
    if _column_exists("olt_devices", "supports_ont_wan_config"):
        op.drop_column("olt_devices", "supports_ont_wan_config")
    if _column_exists("olt_devices", "supports_ont_internet_config"):
        op.drop_column("olt_devices", "supports_ont_internet_config")
