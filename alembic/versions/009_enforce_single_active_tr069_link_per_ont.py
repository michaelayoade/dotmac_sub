"""enforce single active tr069 link per ont

Revision ID: 009_enforce_single_active_tr069_link_per_ont
Revises: 008_add_ont_status_snapshot_fields
Create Date: 2026-04-02
"""

import sqlalchemy as sa

from alembic import op

revision = "009_enforce_single_active_tr069_link_per_ont"
down_revision = "008_add_ont_status_snapshot_fields"
branch_labels = None
depends_on = None


def _index_exists(table_name: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    op.execute(
        """
        WITH ranked_links AS (
            SELECT
                id,
                ont_unit_id,
                ROW_NUMBER() OVER (
                    PARTITION BY ont_unit_id
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                ) AS row_num
            FROM tr069_cpe_devices
            WHERE is_active = TRUE
              AND ont_unit_id IS NOT NULL
        )
        UPDATE tr069_cpe_devices AS device
        SET ont_unit_id = NULL
        FROM ranked_links
        WHERE device.id = ranked_links.id
          AND ranked_links.row_num > 1
        """
    )

    if not _index_exists(
        "tr069_cpe_devices", "uq_tr069_cpe_devices_active_ont_unit_id"
    ):
        op.create_index(
            "uq_tr069_cpe_devices_active_ont_unit_id",
            "tr069_cpe_devices",
            ["ont_unit_id"],
            unique=True,
            postgresql_where=sa.text("is_active AND ont_unit_id IS NOT NULL"),
        )


def downgrade() -> None:
    if _index_exists("tr069_cpe_devices", "uq_tr069_cpe_devices_active_ont_unit_id"):
        op.drop_index(
            "uq_tr069_cpe_devices_active_ont_unit_id",
            table_name="tr069_cpe_devices",
        )
