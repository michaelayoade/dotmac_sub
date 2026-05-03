"""Drop ONT-unit legacy LAN and WiFi config fields.

Revision ID: 085_drop_ont_unit_legacy_lan_wifi
Revises: 084_backfill_ont_desired_config
Create Date: 2026-05-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "085_drop_ont_unit_legacy_lan_wifi"
down_revision = "084_backfill_ont_desired_config"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _drop_column_if_exists(inspector: sa.Inspector, table: str, column: str) -> None:
    if _column_exists(inspector, table, column):
        op.drop_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _column_exists(inspector, "ont_units", "lan_gateway_ip"):
        bind.execute(
            sa.text(
                """
                UPDATE ont_units
                SET desired_config = COALESCE(desired_config, '{}'::jsonb)
                    || jsonb_build_object(
                        'lan',
                        jsonb_strip_nulls(
                            jsonb_build_object(
                                'ip', NULLIF(BTRIM(lan_gateway_ip), ''),
                                'subnet', NULLIF(BTRIM(lan_subnet_mask), ''),
                                'dhcp_enabled', lan_dhcp_enabled,
                                'dhcp_start', NULLIF(BTRIM(lan_dhcp_start), ''),
                                'dhcp_end', NULLIF(BTRIM(lan_dhcp_end), '')
                            )
                        )
                        || COALESCE(desired_config->'lan', '{}'::jsonb)
                    )
                WHERE lan_gateway_ip IS NOT NULL
                   OR lan_subnet_mask IS NOT NULL
                   OR lan_dhcp_enabled IS NOT NULL
                   OR lan_dhcp_start IS NOT NULL
                   OR lan_dhcp_end IS NOT NULL
                """
            )
        )

    for column in (
        "lan_gateway_ip",
        "lan_subnet_mask",
        "lan_dhcp_enabled",
        "lan_dhcp_start",
        "lan_dhcp_end",
        "wifi_ssid",
        "wifi_password",
    ):
        _drop_column_if_exists(inspector, "ont_units", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "ont_units", "lan_gateway_ip"):
        op.add_column("ont_units", sa.Column("lan_gateway_ip", sa.String(64)))
    if not _column_exists(inspector, "ont_units", "lan_subnet_mask"):
        op.add_column("ont_units", sa.Column("lan_subnet_mask", sa.String(64)))
    if not _column_exists(inspector, "ont_units", "lan_dhcp_enabled"):
        op.add_column("ont_units", sa.Column("lan_dhcp_enabled", sa.Boolean()))
    if not _column_exists(inspector, "ont_units", "lan_dhcp_start"):
        op.add_column("ont_units", sa.Column("lan_dhcp_start", sa.String(64)))
    if not _column_exists(inspector, "ont_units", "lan_dhcp_end"):
        op.add_column("ont_units", sa.Column("lan_dhcp_end", sa.String(64)))
