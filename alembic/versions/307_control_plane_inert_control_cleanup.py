"""Remove inert settings and module controls with no canonical consumer.

Revision ID: 282_control_plane_cleanup
Revises: 281_tr181_wan_observed_dns
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "307_control_plane_cleanup"
down_revision = "306_cpe_firmware_identity"
branch_labels = None
depends_on = None

_INERT_SETTINGS = (
    ("subscriber", "account_number_enabled"),
    ("subscriber", "account_number_padding"),
    ("subscriber", "account_number_start"),
    ("network_monitoring", "core_device_ping_interval_seconds"),
    ("network_monitoring", "core_device_snmp_walk_interval_seconds"),
    ("subscriber", "default_account_status"),
    ("subscriber", "default_contact_role"),
    ("inventory", "default_material_status"),
    ("network", "default_olt_port_type"),
    ("inventory", "default_reservation_status"),
    ("network", "default_splitter_input_ports"),
    ("network", "default_splitter_output_ports"),
    ("network", "hotspot_redirect_url"),
    ("network", "hotspot_walled_garden"),
    ("comms", "meta_access_token_override"),
    ("comms", "meta_api_timeout_seconds"),
    ("comms", "meta_oauth_redirect_uri"),
    ("notification", "notification_category_preferences_enabled"),
    ("network_monitoring", "olt_polling_interval_minutes"),
    ("network_monitoring", "ont_offline_poll_threshold"),
    ("network_monitoring", "pon_outage_min_offline_onus"),
    ("network", "vendor_bid_minimum_days"),
    ("network", "vendor_quote_approval_threshold"),
    ("network", "vendor_quote_validity_days"),
    ("auth", "vendor_remember_ttl_seconds"),
    ("auth", "vendor_session_ttl_seconds"),
)

_INERT_MODULE_KEYS = (
    "module_inventory_enabled",
    "module_helpdesk_enabled",
    "module_scheduling_enabled",
    "module_voice_enabled",
)

_DELETE_SETTING = sa.text(
    "DELETE FROM domain_settings WHERE domain = CAST(:domain AS settingdomain) "
    "AND key = :key"
)


def upgrade() -> None:
    for domain, key in _INERT_SETTINGS:
        op.execute(_DELETE_SETTING.bindparams(domain=domain, key=key))
    for key in _INERT_MODULE_KEYS:
        op.execute(_DELETE_SETTING.bindparams(domain="modules", key=key))


def downgrade() -> None:
    # Intentional no-op. Removed values had no consumer, and recreating them
    # would reintroduce controls that falsely imply operational effect.
    pass
