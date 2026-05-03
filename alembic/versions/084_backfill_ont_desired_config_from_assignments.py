"""Backfill ONT desired_config from assignment service config.

Revision ID: 084_backfill_ont_desired_config
Revises: 083_drop_ont_effective_status
Create Date: 2026-05-03
"""

from __future__ import annotations

from alembic import op

revision = "084_backfill_ont_desired_config"
down_revision = "083_drop_ont_effective_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        WITH active_assignment AS (
            SELECT DISTINCT ON (ont_unit_id)
                *
            FROM ont_assignments
            WHERE is_active IS TRUE
            ORDER BY ont_unit_id, updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        ),
        built AS (
            SELECT
                ou.id,
                COALESCE(ou.desired_config, '{}'::jsonb) AS current_config,
                jsonb_strip_nulls(
                    jsonb_build_object(
                        'onu_mode', aa.wan_mode::text,
                        'mode',
                            CASE
                                WHEN NULLIF(BTRIM(aa.pppoe_username), '') IS NOT NULL THEN 'pppoe'
                                WHEN NULLIF(BTRIM(aa.static_ip), '') IS NOT NULL THEN 'static_ip'
                                WHEN aa.wan_mode::text = 'bridging' THEN 'bridge'
                                ELSE aa.ip_mode::text
                            END,
                        'static_ip', NULLIF(BTRIM(aa.static_ip), ''),
                        'static_gateway', NULLIF(BTRIM(aa.static_gateway), ''),
                        'static_subnet', NULLIF(BTRIM(aa.static_subnet), ''),
                        'static_dns', NULLIF(BTRIM(aa.static_dns), ''),
                        'pppoe_username', NULLIF(BTRIM(aa.pppoe_username), ''),
                        'pppoe_password', NULLIF(BTRIM(aa.pppoe_password), '')
                    )
                ) AS wan_config,
                jsonb_strip_nulls(
                    jsonb_build_object(
                        'ip_mode', aa.mgmt_ip_mode::text,
                        'ip_address', NULLIF(BTRIM(aa.mgmt_ip_address), ''),
                        'subnet', NULLIF(BTRIM(aa.mgmt_subnet), ''),
                        'gateway', NULLIF(BTRIM(aa.mgmt_gateway), '')
                    )
                ) AS management_config,
                jsonb_strip_nulls(
                    jsonb_build_object(
                        'ip', NULLIF(BTRIM(aa.lan_ip), ''),
                        'subnet', NULLIF(BTRIM(aa.lan_subnet), ''),
                        'dhcp_enabled', aa.lan_dhcp_enabled,
                        'dhcp_start', NULLIF(BTRIM(aa.lan_dhcp_start), ''),
                        'dhcp_end', NULLIF(BTRIM(aa.lan_dhcp_end), '')
                    )
                ) AS lan_config,
                jsonb_strip_nulls(
                    jsonb_build_object(
                        'enabled', aa.wifi_enabled,
                        'ssid', NULLIF(BTRIM(aa.wifi_ssid), ''),
                        'password', NULLIF(BTRIM(aa.wifi_password), ''),
                        'security_mode', NULLIF(BTRIM(aa.wifi_security_mode), ''),
                        'channel', NULLIF(BTRIM(aa.wifi_channel), '')
                    )
                ) AS wifi_config
            FROM ont_units ou
            JOIN active_assignment aa ON aa.ont_unit_id = ou.id
        ),
        merged AS (
            SELECT
                id,
                current_config
                || jsonb_build_object(
                    'wan', wan_config || COALESCE(current_config->'wan', '{}'::jsonb),
                    'management',
                        management_config
                        || COALESCE(current_config->'management', '{}'::jsonb),
                    'lan', lan_config || COALESCE(current_config->'lan', '{}'::jsonb),
                    'wifi',
                        wifi_config || COALESCE(current_config->'wifi', '{}'::jsonb)
                ) AS next_config
            FROM built
        )
        UPDATE ont_units ou
        SET desired_config = merged.next_config
        FROM merged
        WHERE ou.id = merged.id
          AND ou.desired_config IS DISTINCT FROM merged.next_config
        """
    )
    bind.exec_driver_sql(
        """
        UPDATE ont_assignments
        SET
            wan_mode = NULL,
            ip_mode = NULL,
            static_ip = NULL,
            static_gateway = NULL,
            static_subnet = NULL,
            static_dns = NULL,
            pppoe_username = NULL,
            pppoe_password = NULL,
            mgmt_ip_mode = NULL,
            mgmt_ip_address = NULL,
            mgmt_subnet = NULL,
            mgmt_gateway = NULL,
            lan_ip = NULL,
            lan_subnet = NULL,
            lan_dhcp_enabled = NULL,
            lan_dhcp_start = NULL,
            lan_dhcp_end = NULL,
            wifi_enabled = NULL,
            wifi_ssid = NULL,
            wifi_password = NULL,
            wifi_security_mode = NULL,
            wifi_channel = NULL
        WHERE
            wan_mode IS NOT NULL
            OR ip_mode IS NOT NULL
            OR static_ip IS NOT NULL
            OR static_gateway IS NOT NULL
            OR static_subnet IS NOT NULL
            OR static_dns IS NOT NULL
            OR pppoe_username IS NOT NULL
            OR pppoe_password IS NOT NULL
            OR mgmt_ip_mode IS NOT NULL
            OR mgmt_ip_address IS NOT NULL
            OR mgmt_subnet IS NOT NULL
            OR mgmt_gateway IS NOT NULL
            OR lan_ip IS NOT NULL
            OR lan_subnet IS NOT NULL
            OR lan_dhcp_enabled IS NOT NULL
            OR lan_dhcp_start IS NOT NULL
            OR lan_dhcp_end IS NOT NULL
            OR wifi_enabled IS NOT NULL
            OR wifi_ssid IS NOT NULL
            OR wifi_password IS NOT NULL
            OR wifi_security_mode IS NOT NULL
            OR wifi_channel IS NOT NULL
        """
    )


def downgrade() -> None:
    # Backfilled JSON cannot be distinguished from operator-entered intent.
    pass
