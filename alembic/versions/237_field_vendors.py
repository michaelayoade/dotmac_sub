"""Add field vendor identity and devices.

Revision ID: 237_field_vendors
Revises: 236_field_fiber_tests
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "237_field_vendors"
down_revision = "236_field_fiber_tests"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table("field_vendors"):
        op.create_table(
            "field_vendors",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("crm_vendor_id", sa.String(length=64)),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("code", sa.String(length=60)),
            sa.Column("contact_name", sa.String(length=160)),
            sa.Column("contact_email", sa.String(length=255)),
            sa.Column("contact_phone", sa.String(length=40)),
            sa.Column("service_area", sa.Text()),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("code", name="uq_field_vendors_code"),
            sa.UniqueConstraint("crm_vendor_id", name="uq_field_vendors_crm_vendor_id"),
        )
        op.create_index("ix_field_vendors_active", "field_vendors", ["is_active"])
    if not _has_table("field_vendor_users"):
        op.create_table(
            "field_vendor_users",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("crm_vendor_user_id", sa.String(length=64)),
            sa.Column(
                "vendor_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("field_vendors.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "system_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("system_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(length=60)),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "vendor_id",
                "system_user_id",
                name="uq_field_vendor_users_vendor_system_user",
            ),
            sa.UniqueConstraint(
                "crm_vendor_user_id",
                name="uq_field_vendor_users_crm_vendor_user_id",
            ),
        )
        op.create_index(
            "ix_field_vendor_users_system_user_id",
            "field_vendor_users",
            ["system_user_id"],
        )
        op.create_index(
            "ix_field_vendor_users_active",
            "field_vendor_users",
            ["is_active"],
        )
    if not _has_table("field_vendor_device_tokens"):
        op.create_table(
            "field_vendor_device_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "vendor_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("field_vendor_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("token", sa.String(length=512), nullable=False),
            sa.Column("platform", sa.String(length=16)),
            sa.Column("app_version", sa.String(length=40)),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("token", name="uq_field_vendor_device_tokens_token"),
        )
        op.create_index(
            "ix_field_vendor_device_tokens_vendor_user_id",
            "field_vendor_device_tokens",
            ["vendor_user_id"],
        )
        op.create_index(
            "ix_field_vendor_device_tokens_active",
            "field_vendor_device_tokens",
            ["is_active"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("field_vendor_device_tokens"):
        op.drop_index(
            "ix_field_vendor_device_tokens_active",
            table_name="field_vendor_device_tokens",
        )
        op.drop_index(
            "ix_field_vendor_device_tokens_vendor_user_id",
            table_name="field_vendor_device_tokens",
        )
        op.drop_table("field_vendor_device_tokens")
    if _has_table("field_vendor_users"):
        op.drop_index("ix_field_vendor_users_active", table_name="field_vendor_users")
        op.drop_index(
            "ix_field_vendor_users_system_user_id",
            table_name="field_vendor_users",
        )
        op.drop_table("field_vendor_users")
    if _has_table("field_vendors"):
        op.drop_index("ix_field_vendors_active", table_name="field_vendors")
        op.drop_table("field_vendors")
