"""Add ont_firmware_images table for TR-069 firmware catalog.

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-03-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "k5l6m7n8o9p0"
down_revision = "j4k5l6m7n8o9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not inspector.has_table("ont_firmware_images"):
        op.create_table(
            "ont_firmware_images",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("vendor", sa.String(120), nullable=False),
            sa.Column("model", sa.String(120), nullable=True),
            sa.Column("version", sa.String(120), nullable=False),
            sa.Column("file_url", sa.String(500), nullable=False),
            sa.Column("filename", sa.String(255), nullable=True),
            sa.Column("checksum", sa.String(128), nullable=True),
            sa.Column("file_size_bytes", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("vendor", "model", "version", name="uq_ont_firmware_vendor_model_version"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if inspector.has_table("ont_firmware_images"):
        op.drop_table("ont_firmware_images")
