"""Add authorization_presets table

Revision ID: 051_add_authorization_presets_table
Revises: 050_add_ont_bundle_assignment_models
Create Date: 2026-04-23

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "051_add_authorization_presets_table"
down_revision = "050_add_ont_bundle_assignment_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if table already exists (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "authorization_presets" in inspector.get_table_names():
        return

    op.create_table(
        "authorization_presets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("provisioning_profile_id", sa.UUID(), nullable=True),
        sa.Column("line_profile_id", sa.Integer(), nullable=True),
        sa.Column("service_profile_id", sa.Integer(), nullable=True),
        sa.Column("default_vlan_id", sa.UUID(), nullable=True),
        sa.Column("auto_authorize", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("serial_pattern", sa.String(length=120), nullable=True),
        sa.Column("olt_device_id", sa.UUID(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["default_vlan_id"], ["vlans.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["olt_device_id"], ["olt_devices.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["provisioning_profile_id"],
            ["ont_provisioning_profiles.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_authorization_presets_name"),
    )
    op.create_index(
        op.f("ix_authorization_presets_olt_device_id"),
        "authorization_presets",
        ["olt_device_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_authorization_presets_provisioning_profile_id"),
        "authorization_presets",
        ["provisioning_profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_authorization_presets_provisioning_profile_id"),
        table_name="authorization_presets",
    )
    op.drop_index(
        op.f("ix_authorization_presets_olt_device_id"),
        table_name="authorization_presets",
    )
    op.drop_table("authorization_presets")
