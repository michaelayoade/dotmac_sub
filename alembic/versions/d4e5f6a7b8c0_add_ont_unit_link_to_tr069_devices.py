"""Add explicit ONT link to TR-069 devices.

Revision ID: d4e5f6a7b8c0
Revises: c3d4e5f6a7b9
Create Date: 2026-03-23 14:25:00.000000
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c0"
down_revision = "c3d4e5f6a7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tr069_cpe_devices",
        sa.Column("ont_unit_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_tr069_cpe_devices_ont_unit_id",
        "tr069_cpe_devices",
        "ont_units",
        ["ont_unit_id"],
        ["id"],
    )
    op.create_index(
        "ix_tr069_cpe_devices_ont_unit_id",
        "tr069_cpe_devices",
        ["ont_unit_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tr069_cpe_devices_ont_unit_id", table_name="tr069_cpe_devices")
    op.drop_constraint(
        "fk_tr069_cpe_devices_ont_unit_id",
        "tr069_cpe_devices",
        type_="foreignkey",
    )
    op.drop_column("tr069_cpe_devices", "ont_unit_id")
