"""widen encrypted credential columns

Revision ID: 041_widen_encrypted_credential_columns
Revises: 040_add_bulk_provisioning_audit
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "041_widen_encrypted_credential_columns"
down_revision = "040_add_bulk_provisioning_audit"
branch_labels = None
depends_on = None


UPGRADES: tuple[tuple[str, str, int | None, int], ...] = (
    ("nas_devices", "shared_secret", 255, 512),
    ("nas_devices", "ssh_password", 255, 512),
    ("nas_devices", "api_password", 255, 512),
    ("nas_devices", "snmp_community", 120, 512),
    ("access_credentials", "secret_hash", 255, 512),
    ("olt_devices", "ssh_password", 255, 512),
    ("olt_devices", "snmp_ro_community", 255, 512),
    ("olt_devices", "snmp_rw_community", 255, 512),
    ("network_devices", "snmp_community", 255, 512),
    ("network_devices", "snmp_rw_community", 255, 512),
    ("network_devices", "snmp_auth_secret", 255, 512),
    ("network_devices", "snmp_priv_secret", 255, 512),
    ("ont_units", "pppoe_password", 120, 512),
    ("ont_units", "wifi_password", 120, 512),
    ("ont_provisioning_profiles", "cr_password", 120, 512),
    ("ont_profile_wan_services", "pppoe_static_password", 500, 512),
    ("ont_wan_service_instances", "pppoe_password", 500, 512),
    ("tr069_acs_servers", "cwmp_password", 255, 512),
    ("tr069_acs_servers", "connection_request_password", 255, 512),
    ("webhook_endpoints", "secret", 255, 512),
    ("payment_methods", "token", 255, 512),
    ("bank_accounts", "token", 255, 512),
)


def _alter_columns(direction: str) -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table_name, column_name, old_length, new_length in UPGRADES:
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name not in columns:
            continue
        if direction == "upgrade":
            existing_length = old_length
            target_length = new_length
        else:
            existing_length = new_length
            target_length = old_length
        op.alter_column(
            table_name,
            column_name,
            existing_type=sa.String(length=existing_length),
            type_=sa.String(length=target_length),
            existing_nullable=True,
        )


def upgrade() -> None:
    _alter_columns("upgrade")


def downgrade() -> None:
    _alter_columns("downgrade")
