"""Add UISP desired/observed intents and config snapshots.

Revision ID: 266_uisp_control_plane
Revises: 265_uisp_subscription_ownership
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "266_uisp_control_plane"
down_revision = "265_uisp_subscription_ownership"
branch_labels = None
depends_on = None


_UISP_CAPABILITIES = (
    {
        "id": uuid.UUID("26600000-0000-4000-8000-000000000001"),
        "vendor": "Ubiquiti",
        "model": "LBE-5AC-Gen2",
        "supported_features": {
            "uisp": {
                "configuration_write": True,
                "transport": "airos",
                "fields": {"name": "/system/hostname"},
            }
        },
        "max_ssids": 0,
        "supports_ipv6": False,
        "notes": "UISP airOS read/write verified; hostname is the safe SoT canary field.",
    },
    {
        "id": uuid.UUID("26600000-0000-4000-8000-000000000002"),
        "vendor": "Ubiquiti",
        "model": "UF-Wifi",
        "supported_features": {
            "uisp": {
                "configuration_write": True,
                "transport": "onu",
                "fields": {
                    "wifi.ssid": "/wireless/ssid",
                    "wifi.password_ref": "/wireless/key",
                    "remote_access.enabled": "/services/sshEnabled",
                },
            }
        },
        "max_ssids": 1,
        "supports_ipv6": True,
        "notes": "UISP UFiber write/readback/restore verified on production hardware.",
    },
    {
        "id": uuid.UUID("26600000-0000-4000-8000-000000000003"),
        "vendor": "Ubiquiti",
        "model": "UF-Wifi6",
        "supported_features": {
            "uisp": {
                "configuration_write": True,
                "transport": "onu",
                "fields": {
                    "wifi.ssid": "/wireless/ssid",
                    "wifi.password_ref": "/wireless/key",
                    "remote_access.enabled": "/services/sshEnabled",
                },
            }
        },
        "max_ssids": 1,
        "supports_ipv6": True,
        "notes": "UF-Wifi6 uses UISP's UFiber configuration transport.",
    },
)


def _seed_uisp_capabilities() -> None:
    table = sa.table(
        "vendor_model_capabilities",
        sa.column("id", sa.UUID()),
        sa.column("vendor", sa.String()),
        sa.column("model", sa.String()),
        sa.column("firmware_pattern", sa.String()),
        sa.column("tr069_root", sa.String()),
        sa.column("supported_features", sa.JSON()),
        sa.column("max_wan_services", sa.Integer()),
        sa.column("max_lan_ports", sa.Integer()),
        sa.column("max_ssids", sa.Integer()),
        sa.column("supports_vlan_tagging", sa.Boolean()),
        sa.column("supports_qinq", sa.Boolean()),
        sa.column("supports_ipv6", sa.Boolean()),
        sa.column("is_active", sa.Boolean()),
        sa.column("notes", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    now = datetime.now(UTC)
    for seed in _UISP_CAPABILITIES:
        exists = bind.scalar(
            sa.select(sa.literal(True)).where(
                sa.exists(
                    sa.select(sa.literal(1))
                    .select_from(table)
                    .where(
                        sa.func.lower(table.c.vendor) == str(seed["vendor"]).lower(),
                        sa.func.lower(table.c.model) == str(seed["model"]).lower(),
                    )
                )
            )
        )
        if exists:
            continue
        bind.execute(
            sa.insert(table).values(
                **seed,
                firmware_pattern=None,
                tr069_root=None,
                max_wan_services=1,
                max_lan_ports=4,
                supports_vlan_tagging=True,
                supports_qinq=False,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )


def upgrade() -> None:
    op.create_table(
        "uisp_device_intents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("target_type", sa.String(length=3), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=True),
        sa.Column("service_order_id", sa.UUID(), nullable=True),
        sa.Column("uisp_device_id", sa.String(length=120), nullable=True),
        sa.Column("desired_state", sa.JSON(), nullable=False),
        sa.Column("observed_config", sa.JSON(), nullable=True),
        sa.Column("drift", sa.JSON(), nullable=True),
        sa.Column("desired_revision", sa.Integer(), nullable=False),
        sa.Column("verified_revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["service_order_id"], ["service_orders.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_type", "target_id", name="uq_uisp_intent_target"),
    )
    op.create_index(
        "ix_uisp_intent_subscription",
        "uisp_device_intents",
        ["subscription_id"],
    )
    op.create_index(
        "ix_uisp_intent_service_order_id",
        "uisp_device_intents",
        ["service_order_id"],
    )
    op.create_index(
        "ix_uisp_intent_uisp_device_id",
        "uisp_device_intents",
        ["uisp_device_id"],
    )
    op.create_index("ix_uisp_intent_status", "uisp_device_intents", ["status"])

    op.create_table(
        "uisp_config_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("intent_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["uisp_device_intents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_uisp_snapshot_intent_created",
        "uisp_config_snapshots",
        ["intent_id", "created_at"],
    )
    _seed_uisp_capabilities()


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM vendor_model_capabilities "
            "WHERE id IN ('26600000-0000-4000-8000-000000000001', "
            "'26600000-0000-4000-8000-000000000002', "
            "'26600000-0000-4000-8000-000000000003')"
        )
    )
    op.drop_index("ix_uisp_snapshot_intent_created", table_name="uisp_config_snapshots")
    op.drop_table("uisp_config_snapshots")
    op.drop_index("ix_uisp_intent_status", table_name="uisp_device_intents")
    op.drop_index("ix_uisp_intent_uisp_device_id", table_name="uisp_device_intents")
    op.drop_index("ix_uisp_intent_service_order_id", table_name="uisp_device_intents")
    op.drop_index("ix_uisp_intent_subscription", table_name="uisp_device_intents")
    op.drop_table("uisp_device_intents")
