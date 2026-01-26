"""Add keep_forever flag to NAS config backups.

Revision ID: 8c1d5f0a2b7e
Revises: 6b2d9f4a8c3e
Create Date: 2026-01-14

"""
from alembic import op
import sqlalchemy as sa

revision = "8c1d5f0a2b7e"
down_revision = "6b2d9f4a8c3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("nas_config_backups")}
    if "keep_forever" not in columns:
        op.add_column(
            "nas_config_backups",
            sa.Column("keep_forever", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )


def downgrade() -> None:
    op.drop_column("nas_config_backups", "keep_forever")
