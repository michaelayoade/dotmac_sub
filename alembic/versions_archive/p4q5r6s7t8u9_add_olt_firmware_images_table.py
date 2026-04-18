"""Add OLT firmware images table.

Revision ID: p4q5r6s7t8u9
Revises: o3p4q5r6s7t8
Create Date: 2026-03-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "p4q5r6s7t8u9"
down_revision = "o3p4q5r6s7t8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _table_exists("olt_firmware_images"):
        op.create_table(
            "olt_firmware_images",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("vendor", sa.String(120), nullable=False),
            sa.Column("model", sa.String(120), nullable=True),
            sa.Column("version", sa.String(120), nullable=False),
            sa.Column("file_url", sa.String(500), nullable=False),
            sa.Column("filename", sa.String(255), nullable=True),
            sa.Column("checksum", sa.String(128), nullable=True),
            sa.Column("file_size_bytes", sa.Integer, nullable=True),
            sa.Column("release_notes", sa.Text, nullable=True),
            sa.Column("upgrade_method", sa.String(60), nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("vendor", "model", "version", name="uq_olt_firmware_vendor_model_version"),
        )


def downgrade() -> None:
    op.drop_table("olt_firmware_images")


def _table_exists(table_name: str) -> bool:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    inspector = sa_inspect(bind)
    return table_name in inspector.get_table_names()
