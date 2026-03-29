"""Ensure only one current backup per NAS device.

Revision ID: 6b2d9f4a8c3e
Revises: 4f7b2a1c9e2d
Create Date: 2026-01-14

"""
from alembic import op
import sqlalchemy as sa

revision = "6b2d9f4a8c3e"
down_revision = "4f7b2a1c9e2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("nas_config_backups")}
    if "uq_nas_config_backups_current" not in indexes:
        op.create_index(
            "uq_nas_config_backups_current",
            "nas_config_backups",
            ["nas_device_id"],
            unique=True,
            postgresql_where=sa.text("is_current"),
        )


def downgrade() -> None:
    op.drop_index("uq_nas_config_backups_current", table_name="nas_config_backups")
