"""Add per-device verification flags for NAS API/SSH.

Revision ID: 4f7b2a1c9e2d
Revises: 1f2a3c4d5e6f
Create Date: 2026-01-14

"""
from alembic import op
import sqlalchemy as sa

revision = "4f7b2a1c9e2d"
down_revision = "c106b739a7ee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("nas_devices")}
    if "ssh_verify_host_key" not in columns:
        op.add_column(
            "nas_devices",
            sa.Column("ssh_verify_host_key", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if "api_verify_tls" not in columns:
        op.add_column(
            "nas_devices",
            sa.Column("api_verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )


def downgrade() -> None:
    op.drop_column("nas_devices", "api_verify_tls")
    op.drop_column("nas_devices", "ssh_verify_host_key")
