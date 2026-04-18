"""add genieacs_device_id to tr069_cpe_devices

Revision ID: ffdddc71211e
Revises: 007_acs_periodic_inform_interval
Create Date: 2026-04-02 10:39:27.713403

"""

import sqlalchemy as sa

from alembic import op

revision = "ffdddc71211e"
down_revision = "007_acs_periodic_inform_interval"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    # Add genieacs_device_id column
    if not _column_exists("tr069_cpe_devices", "genieacs_device_id"):
        op.add_column(
            "tr069_cpe_devices",
            sa.Column("genieacs_device_id", sa.String(255), nullable=True),
        )
    # Add index for fast lookups
    if not _index_exists(
        "tr069_cpe_devices", "ix_tr069_cpe_devices_genieacs_device_id"
    ):
        op.create_index(
            "ix_tr069_cpe_devices_genieacs_device_id",
            "tr069_cpe_devices",
            ["genieacs_device_id"],
        )

    # Populate existing records from oui-product_class-serial_number
    # Only for records that have all three values
    op.execute(
        """
        UPDATE tr069_cpe_devices
        SET genieacs_device_id = oui || '-' || product_class || '-' || serial_number
        WHERE oui IS NOT NULL AND oui != ''
          AND product_class IS NOT NULL AND product_class != ''
          AND serial_number IS NOT NULL AND serial_number != ''
        """
    )


def downgrade() -> None:
    if _index_exists("tr069_cpe_devices", "ix_tr069_cpe_devices_genieacs_device_id"):
        op.drop_index("ix_tr069_cpe_devices_genieacs_device_id", "tr069_cpe_devices")
    if _column_exists("tr069_cpe_devices", "genieacs_device_id"):
        op.drop_column("tr069_cpe_devices", "genieacs_device_id")
