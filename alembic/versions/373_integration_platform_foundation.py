"""Add versioned integration installations and capability bindings.

This migration establishes the replacement control plane directly. It does not
store compatibility pointers to the retired connector configuration system.

Revision ID: 373_integration_platform_foundation
Revises: 372_vendor_payment_projection
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "373_integration_platform_foundation"
down_revision = "372_vendor_payment_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_installations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_key", sa.String(length=120), nullable=False),
        sa.Column("connector_version", sa.String(length=32), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "environment",
            sa.String(length=24),
            server_default="production",
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.String(length=24),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("state_reason", sa.Text(), nullable=True),
        # The named FK is added after integration_config_revisions exists.
        sa.Column(
            "current_config_revision_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.Column("updated_by", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'validating', 'disabled', 'enabled', "
            "'quarantined', 'retired')",
            name="ck_integration_installations_state",
        ),
        sa.CheckConstraint(
            "environment IN ('production', 'sandbox', 'test')",
            name="ck_integration_installations_environment",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_installations"),
        sa.UniqueConstraint(
            "connector_key",
            "name",
            name="uq_integration_installations_connector_name",
        ),
    )
    op.create_index(
        "ix_integration_installations_key_state",
        "integration_installations",
        ["connector_key", "state"],
    )

    op.create_table(
        "integration_config_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(length=32),
            server_default="v1",
            nullable=False,
        ),
        sa.Column("config_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("secret_refs", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("config_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "validation_status",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("validation_errors", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'valid', 'invalid')",
            name="ck_integration_config_revisions_validation_status",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["integration_installations.id"],
            name="fk_integration_config_revisions_installation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_config_revisions"),
        sa.UniqueConstraint(
            "installation_id",
            "revision",
            name="uq_integration_config_revisions_installation_revision",
        ),
        sa.UniqueConstraint(
            "installation_id",
            "config_digest",
            name="uq_integration_config_revisions_installation_digest",
        ),
    )
    op.create_index(
        "ix_integration_config_revisions_installation_created",
        "integration_config_revisions",
        ["installation_id", "created_at"],
    )
    op.create_foreign_key(
        "fk_integration_installations_current_config_revision",
        "integration_installations",
        "integration_config_revisions",
        ["current_config_revision_id"],
        ["id"],
    )

    op.create_table(
        "integration_capability_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_id", sa.String(length=160), nullable=False),
        sa.Column(
            "state",
            sa.String(length=24),
            server_default="disabled",
            nullable=False,
        ),
        sa.Column("scope_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("policy_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.Column("updated_by", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('disabled', 'enabled')",
            name="ck_integration_capability_bindings_state",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["integration_installations.id"],
            name="fk_integration_capability_bindings_installation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_capability_bindings"),
        sa.UniqueConstraint(
            "installation_id",
            "capability_id",
            name="uq_integration_capability_bindings_installation_capability",
        ),
    )
    op.create_index(
        "ix_integration_capability_bindings_capability_state",
        "integration_capability_bindings",
        ["capability_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_capability_bindings_capability_state",
        table_name="integration_capability_bindings",
    )
    op.drop_table("integration_capability_bindings")
    op.drop_constraint(
        "fk_integration_installations_current_config_revision",
        "integration_installations",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_integration_config_revisions_installation_created",
        table_name="integration_config_revisions",
    )
    op.drop_table("integration_config_revisions")
    op.drop_index(
        "ix_integration_installations_key_state",
        table_name="integration_installations",
    )
    op.drop_table("integration_installations")
