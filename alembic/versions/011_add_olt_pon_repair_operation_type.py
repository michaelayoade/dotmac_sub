"""add olt pon repair operation type

Revision ID: 011_add_olt_pon_repair_operation_type
Revises: 010_add_ont_tr069_snapshot_cache
Create Date: 2026-04-03
"""

from alembic import op

revision = "011_add_olt_pon_repair_operation_type"
down_revision = "010_add_ont_tr069_snapshot_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE networkoperationtype ADD VALUE IF NOT EXISTS 'olt_pon_repair'"
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE network_operations
        SET operation_type = 'olt_ont_sync'
        WHERE operation_type = 'olt_pon_repair'
        """
    )
    op.execute("ALTER TYPE networkoperationtype RENAME TO networkoperationtype_old")
    op.execute(
        """
        CREATE TYPE networkoperationtype AS ENUM (
            'olt_ont_sync',
            'ont_authorize',
            'ont_reboot',
            'ont_factory_reset',
            'ont_set_pppoe',
            'ont_set_conn_request_creds',
            'ont_send_conn_request',
            'ont_enable_ipv6',
            'cpe_set_conn_request_creds',
            'cpe_send_conn_request',
            'cpe_reboot',
            'cpe_factory_reset',
            'tr069_bootstrap',
            'wifi_update',
            'pppoe_push',
            'router_config_push',
            'router_config_backup',
            'router_reboot',
            'router_firmware_upgrade',
            'router_bulk_push'
        )
        """
    )
    op.execute(
        """
        ALTER TABLE network_operations
        ALTER COLUMN operation_type
        TYPE networkoperationtype
        USING operation_type::text::networkoperationtype
        """
    )
    op.execute("DROP TYPE networkoperationtype_old")
