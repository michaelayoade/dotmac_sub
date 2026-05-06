"""Add OLT WAN provisioning mode.

Revision ID: 088_add_olt_wan_provisioning_mode
Revises: add_olt_capability_flags
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "088_add_olt_wan_provisioning_mode"
down_revision = "add_olt_capability_flags"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return constraint_name in {
        constraint["name"]
        for constraint in inspector.get_check_constraints(table_name)
    }


def upgrade() -> None:
    if not _column_exists("olt_devices", "supports_ont_home_gateway_config"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "supports_ont_home_gateway_config",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            ),
        )

    if not _column_exists("olt_devices", "wan_provisioning_mode"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "wan_provisioning_mode",
                sa.String(length=40),
                nullable=False,
                server_default="omci_wan_config",
            ),
        )

    if not _column_exists("olt_devices", "capabilities_source"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "capabilities_source",
                sa.String(length=20),
                nullable=False,
                server_default="auto",
            ),
        )

    op.execute(
        """
        UPDATE olt_devices
        SET supports_ont_internet_config = false,
            supports_ont_wan_config = false,
            supports_ont_home_gateway_config = false,
            wan_provisioning_mode = 'tr069_only'
        WHERE model ILIKE '%MA5608T%'
          AND (
            firmware_version ILIKE '%V800R013%'
            OR software_version ILIKE '%V800R013%'
          )
        """
    )

    op.execute(
        """
        UPDATE olt_devices
        SET supports_ont_internet_config = false,
            supports_ont_wan_config = false,
            supports_ont_home_gateway_config = true,
            wan_provisioning_mode = 'home_gateway_config'
        WHERE model ILIKE '%MA5608T%'
          AND (
            firmware_version ILIKE '%V800R015%'
            OR software_version ILIKE '%V800R015%'
          )
        """
    )

    op.execute(
        """
        UPDATE olt_devices
        SET supports_ont_internet_config = true,
            supports_ont_wan_config = true,
            supports_ont_home_gateway_config = true,
            wan_provisioning_mode = 'omci_wan_config'
        WHERE model ILIKE '%MA5608T%'
          AND (
            firmware_version ILIKE '%V800R018%'
            OR firmware_version ILIKE '%V800R019%'
            OR software_version ILIKE '%V800R018%'
            OR software_version ILIKE '%V800R019%'
          )
        """
    )

    op.execute(
        """
        UPDATE olt_devices
        SET supports_ont_internet_config = true,
            supports_ont_wan_config = true,
            supports_ont_home_gateway_config = false,
            wan_provisioning_mode = 'omci_wan_config'
        WHERE model ILIKE '%MA5800%'
          AND (
            firmware_version ILIKE '%V100R019%'
            OR firmware_version ILIKE '%V800R019%'
            OR software_version ILIKE '%V100R019%'
            OR software_version ILIKE '%V800R019%'
          )
        """
    )

    if not _constraint_exists("olt_devices", "ck_olt_devices_wan_provisioning_mode"):
        op.create_check_constraint(
            "ck_olt_devices_wan_provisioning_mode",
            "olt_devices",
            "wan_provisioning_mode IN "
            "('tr069_only', 'home_gateway_config', 'omci_wan_config')",
        )
    if not _constraint_exists("olt_devices", "ck_olt_devices_capabilities_source"):
        op.create_check_constraint(
            "ck_olt_devices_capabilities_source",
            "olt_devices",
            "capabilities_source IN ('auto', 'manual')",
        )


def downgrade() -> None:
    if _constraint_exists("olt_devices", "ck_olt_devices_capabilities_source"):
        op.drop_constraint(
            "ck_olt_devices_capabilities_source",
            "olt_devices",
            type_="check",
        )
    if _constraint_exists("olt_devices", "ck_olt_devices_wan_provisioning_mode"):
        op.drop_constraint(
            "ck_olt_devices_wan_provisioning_mode",
            "olt_devices",
            type_="check",
        )
    if _column_exists("olt_devices", "wan_provisioning_mode"):
        op.drop_column("olt_devices", "wan_provisioning_mode")
    if _column_exists("olt_devices", "capabilities_source"):
        op.drop_column("olt_devices", "capabilities_source")
    if _column_exists("olt_devices", "supports_ont_home_gateway_config"):
        op.drop_column("olt_devices", "supports_ont_home_gateway_config")
