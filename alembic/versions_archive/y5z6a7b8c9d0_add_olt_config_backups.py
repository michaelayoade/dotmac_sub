"""Add olt_config_backups table.

Revision ID: y5z6a7b8c9d0
Revises: x4y5z6a7b8c9
Create Date: 2026-02-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, UUID

from alembic import op

revision = "y5z6a7b8c9d0"
down_revision = "x4y5z6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Create the enum type idempotently
    op.execute(
        "DO $$ BEGIN "
        "  CREATE TYPE oltconfigbackuptype AS ENUM ('auto', 'manual'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; "
        "END $$"
    )

    if not inspector.has_table("olt_config_backups"):
        op.create_table(
            "olt_config_backups",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "olt_device_id",
                UUID(as_uuid=True),
                sa.ForeignKey("olt_devices.id"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "backup_type",
                ENUM(
                    "auto",
                    "manual",
                    name="oltconfigbackuptype",
                    create_type=False,
                ),
                nullable=False,
                server_default="auto",
            ),
            sa.Column("file_path", sa.String(512), nullable=False),
            sa.Column("file_size_bytes", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if inspector.has_table("olt_config_backups"):
        op.drop_table("olt_config_backups")

    op.execute("DROP TYPE IF EXISTS oltconfigbackuptype")
