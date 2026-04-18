"""add_tr069_profile_to_olts

Revision ID: h1a2b3c4d5e6
Revises: r8s9t0u1v2w3
Create Date: 2026-03-09 12:05:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "h1a2b3c4d5e6"
down_revision = "r8s9t0u1v2w3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("olt_devices")}

    if "tr069_acs_server_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column("tr069_acs_server_id", postgresql.UUID(as_uuid=True), nullable=True),
        )

    existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys("olt_devices")}
    if "fk_olt_devices_tr069_acs_id" not in existing_fks:
        op.create_foreign_key(
            "fk_olt_devices_tr069_acs_id",
            "olt_devices",
            "tr069_acs_servers",
            ["tr069_acs_server_id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("olt_devices")}
    existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys("olt_devices")}

    if "fk_olt_devices_tr069_acs_id" in existing_fks:
        op.drop_constraint("fk_olt_devices_tr069_acs_id", "olt_devices", type_="foreignkey")

    if "tr069_acs_server_id" in existing_columns:
        op.drop_column("olt_devices", "tr069_acs_server_id")
