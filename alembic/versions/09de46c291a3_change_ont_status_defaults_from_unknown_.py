"""change ont status defaults from unknown to offline

Revision ID: 09de46c291a3
Revises: 081_add_autofind_scan_network_operation_type
Create Date: 2026-04-30 20:03:40.412793

"""

import sqlalchemy as sa

from alembic import op

revision = "09de46c291a3"
down_revision = "081_add_autofind_scan_network_operation_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ont_units",
        "olt_status",
        server_default=sa.text("'offline'::onuonlinestatus"),
    )
    op.alter_column(
        "ont_units",
        "effective_status",
        server_default=sa.text("'offline'::onteffectivestatus"),
    )


def downgrade() -> None:
    op.alter_column(
        "ont_units",
        "olt_status",
        server_default=sa.text("'unknown'::onuonlinestatus"),
    )
    op.alter_column(
        "ont_units",
        "effective_status",
        server_default=sa.text("'unknown'::onteffectivestatus"),
    )
