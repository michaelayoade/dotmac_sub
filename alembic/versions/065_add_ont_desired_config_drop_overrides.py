"""Add ONT desired_config and drop override/saga tables

Revision ID: 065_add_ont_desired_config_drop_overrides
Revises: 064_drop_legacy_ont_config_fields
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "065_add_ont_desired_config_drop_overrides"
down_revision = "064_drop_legacy_ont_config_fields"
branch_labels = None
depends_on = None


FIELD_PATHS = {
    "config_method": ("device", "config_method"),
    "onu_mode": ("device", "onu_mode"),
    "ip_protocol": ("wan", "ip_protocol"),
    "wan.wan_mode": ("wan", "mode"),
    "wan_mode": ("wan", "mode"),
    "wan.vlan_tag": ("wan", "vlan"),
    "wan_vlan": ("wan", "vlan"),
    "wan.pppoe_username": ("wan", "pppoe_username"),
    "pppoe_username": ("wan", "pppoe_username"),
    "wan.pppoe_password": ("wan", "pppoe_password"),
    "pppoe_password": ("wan", "pppoe_password"),
    "management.ip_mode": ("management", "ip_mode"),
    "mgmt_ip_mode": ("management", "ip_mode"),
    "management.vlan_tag": ("management", "vlan"),
    "mgmt_vlan": ("management", "vlan"),
    "management.ip_address": ("management", "ip_address"),
    "mgmt_ip_address": ("management", "ip_address"),
    "wifi.enabled": ("wifi", "enabled"),
    "wifi_enabled": ("wifi", "enabled"),
    "wifi.ssid": ("wifi", "ssid"),
    "wifi_ssid": ("wifi", "ssid"),
    "wifi.password": ("wifi", "password"),
    "wifi_password": ("wifi", "password"),
    "wifi.channel": ("wifi", "channel"),
    "wifi_channel": ("wifi", "channel"),
    "wifi.security_mode": ("wifi", "security_mode"),
    "wifi_security_mode": ("wifi", "security_mode"),
}


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _json_path(path: tuple[str, ...]) -> str:
    return "{" + ",".join(path) + "}"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "ont_units", "desired_config"):
        op.add_column(
            "ont_units",
            sa.Column(
                "desired_config",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    if "ont_config_overrides" in inspector.get_table_names():
        rows = bind.execute(
            sa.text(
                """
                SELECT ont_unit_id, field_name, value_json
                FROM ont_config_overrides
                ORDER BY ont_unit_id, field_name
                """
            )
        ).mappings()
        for row in rows:
            path = FIELD_PATHS.get(str(row["field_name"]))
            if not path:
                path = tuple(
                    part for part in str(row["field_name"]).split(".") if part
                )
            if not path:
                continue
            value_json = row["value_json"] or {}
            value = value_json.get("value") if isinstance(value_json, dict) else value_json
            if value in (None, ""):
                continue
            bind.execute(
                sa.text(
                    """
                    UPDATE ont_units
                    SET desired_config = jsonb_set(
                        COALESCE(desired_config, '{}'::jsonb),
                        CAST(:path AS text[]),
                        to_jsonb(CAST(:value AS text)),
                        true
                    )
                    WHERE id = :ont_unit_id
                    """
                ),
                {
                    "path": _json_path(path),
                    "value": str(value),
                    "ont_unit_id": row["ont_unit_id"],
                },
            )

        for index in inspector.get_indexes("ont_config_overrides"):
            if index.get("name"):
                op.drop_index(index["name"], table_name="ont_config_overrides")
        op.drop_table("ont_config_overrides")

    if "provisioning_step_executions" in inspector.get_table_names():
        for index in inspector.get_indexes("provisioning_step_executions"):
            if index.get("name"):
                op.drop_index(index["name"], table_name="provisioning_step_executions")
        op.drop_table("provisioning_step_executions")

    if "saga_executions" in inspector.get_table_names():
        for index in inspector.get_indexes("saga_executions"):
            if index.get("name"):
                op.drop_index(index["name"], table_name="saga_executions")
        op.drop_table("saga_executions")

    bind.execute(sa.text("DROP TYPE IF EXISTS ontconfigoverridesource"))
    bind.execute(sa.text("DROP TYPE IF EXISTS ontbundleassignmentstatus"))
    bind.execute(sa.text("DROP TYPE IF EXISTS provisioningstepexecutionstatus"))
    bind.execute(sa.text("DROP TYPE IF EXISTS sagaexecutionstatus"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "ont_config_overrides" not in inspector.get_table_names():
        bind.execute(
            sa.text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ontconfigoverridesource') THEN
                        CREATE TYPE ontconfigoverridesource AS ENUM ('operator', 'workflow', 'subscriber_data');
                    END IF;
                END
                $$;
                """
            )
        )
        op.create_table(
            "ont_config_overrides",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "ont_unit_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("ont_units.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("field_name", sa.String(120), nullable=False),
            sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column(
                "source",
                postgresql.ENUM(name="ontconfigoverridesource", create_type=False),
                nullable=False,
                server_default="operator",
            ),
            sa.Column("reason", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "ont_unit_id",
                "field_name",
                name="uq_ont_config_overrides_ont_field",
            ),
        )
        op.create_index(
            "ix_ont_config_overrides_field_name",
            "ont_config_overrides",
            ["field_name"],
        )

    if _column_exists(inspector, "ont_units", "desired_config"):
        op.drop_column("ont_units", "desired_config")
