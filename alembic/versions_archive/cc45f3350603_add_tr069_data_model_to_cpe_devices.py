"""add tr069_data_model to cpe_devices

Revision ID: cc45f3350603
Revises: u7v8w9x0y1z2
Create Date: 2026-03-19 12:26:38.542282

"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "cc45f3350603"
down_revision = "u7v8w9x0y1z2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_columns = [c["name"] for c in inspector.get_columns("cpe_devices")]
    if "tr069_data_model" not in existing_columns:
        op.add_column(
            "cpe_devices",
            sa.Column("tr069_data_model", sa.String(40), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_columns = [c["name"] for c in inspector.get_columns("cpe_devices")]
    if "tr069_data_model" in existing_columns:
        op.drop_column("cpe_devices", "tr069_data_model")
