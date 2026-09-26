"""Add governed custom-field definitions, values, and permissions.

Revision ID: 623_custom_fields_center
Revises: 622_enforcement_applications
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "623_custom_fields_center"
down_revision: str | None = "622_enforcement_applications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("custom_field_definitions", "custom_field_values")
_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("custom_fields:hub:read", "Open the Custom Fields Center"),
    ("custom_fields:definition:read", "View custom-field definitions"),
    ("custom_fields:definition:create", "Create custom-field drafts"),
    ("custom_fields:definition:update", "Update custom-field definitions"),
    ("custom_fields:definition:activate", "Activate custom-field definitions"),
    ("custom_fields:definition:retire", "Retire custom-field definitions"),
    ("custom_fields:value:read", "View custom-field values"),
    ("custom_fields:value:write", "Set custom-field values"),
    ("custom_fields:sensitive:read", "View sensitive custom-field values"),
    ("custom_fields:sensitive:write", "Set sensitive custom-field values"),
)
_GRANT_TABLES = (
    "role_permissions",
    "subscriber_permissions",
    "system_user_permissions",
)


def _permission_id(bind, key: str) -> str | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def _seed_permissions(bind) -> None:
    if "permissions" not in set(sa.inspect(bind).get_table_names()):
        return
    now = datetime.now(UTC)
    for key, description in _PERMISSIONS:
        permission_id = _permission_id(bind, key)
        if permission_id is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO permissions (
                        id, key, description, is_active, is_ui_assignable,
                        created_at, updated_at
                    ) VALUES (:id, :key, :description, true, true, :now, :now)
                    """
                ),
                {
                    "id": str(uuid4()),
                    "key": key,
                    "description": description,
                    "now": now,
                },
            )
        else:
            bind.execute(
                sa.text(
                    """
                    UPDATE permissions
                    SET description = :description,
                        is_active = true,
                        is_ui_assignable = true,
                        updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"id": permission_id, "description": description, "now": now},
            )


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
          USING (tenant_id = app_current_tenant_id())
          WITH CHECK (tenant_id = app_current_tenant_id())
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO platform_api")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "custom_field_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("target_type", sa.String(length=120), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("field_type", sa.String(length=24), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="draft", nullable=False
        ),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "validation",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "default_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("required", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "section",
            sa.String(length=120),
            server_default="Additional information",
            nullable=False,
        ),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "show_in_list", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "show_in_form", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "show_in_detail", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
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
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(trim(target_type)) > 0", name="ck_custom_field_target"
        ),
        sa.CheckConstraint("length(trim(key)) > 0", name="ck_custom_field_key"),
        sa.CheckConstraint("length(trim(label)) > 0", name="ck_custom_field_label"),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_custom_field_definition_status",
        ),
        sa.CheckConstraint(
            "field_type IN ('text', 'textarea', 'integer', 'decimal', 'boolean', "
            "'date', 'datetime', 'select', 'multiselect', 'email', 'url', "
            "'phone', 'currency')",
            name="ck_custom_field_definition_type",
        ),
        sa.CheckConstraint("display_order >= 0", name="ck_custom_field_display_order"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_custom_field_definitions_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "target_type",
            "key",
            name="uq_custom_field_definitions_tenant_target_key",
        ),
    )
    op.create_index(
        "ix_custom_field_definitions_tenant_id",
        "custom_field_definitions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_custom_field_definitions_target_type",
        "custom_field_definitions",
        ["target_type"],
    )
    op.create_index(
        "ix_custom_field_definitions_status", "custom_field_definitions", ["status"]
    )

    op.create_table(
        "custom_field_values",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("definition_id", sa.UUID(), nullable=False),
        sa.Column("target_type", sa.String(length=120), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
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
            "length(trim(target_type)) > 0", name="ck_custom_value_target"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "definition_id"],
            ["custom_field_definitions.tenant_id", "custom_field_definitions.id"],
            ondelete="CASCADE",
            name="fk_custom_field_values_definition_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "definition_id",
            "target_id",
            name="uq_custom_field_values_tenant_definition_target",
        ),
    )
    op.create_index(
        "ix_custom_field_values_tenant_id", "custom_field_values", ["tenant_id"]
    )
    op.create_index(
        "ix_custom_field_values_target",
        "custom_field_values",
        ["tenant_id", "target_type", "target_id"],
    )
    op.create_index(
        "ix_custom_field_values_definition_id", "custom_field_values", ["definition_id"]
    )
    op.execute(
        """
        CREATE FUNCTION guard_custom_field_definition_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'custom-field definitions are retired, not deleted';
          END IF;
          IF OLD.status = 'retired' THEN
            RAISE EXCEPTION 'retired custom-field definitions are immutable';
          END IF;
          IF OLD.status = 'active' AND (
            NEW.target_type IS DISTINCT FROM OLD.target_type OR
            NEW.key IS DISTINCT FROM OLD.key OR
            NEW.field_type IS DISTINCT FROM OLD.field_type OR
            NEW.options IS DISTINCT FROM OLD.options OR
            NEW.validation IS DISTINCT FROM OLD.validation OR
            NEW.default_value IS DISTINCT FROM OLD.default_value OR
            NEW.required IS DISTINCT FROM OLD.required OR
            NEW.sensitive IS DISTINCT FROM OLD.sensitive
          ) THEN
            RAISE EXCEPTION 'active custom-field definition shape is immutable';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER custom_field_definition_lifecycle_guard
        BEFORE UPDATE OR DELETE ON custom_field_definitions
        FOR EACH ROW EXECUTE FUNCTION guard_custom_field_definition_lifecycle()
        """
    )
    for table in _TABLES:
        _enable_rls(table)
    _seed_permissions(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    if "permissions" in set(sa.inspect(bind).get_table_names()):
        table_names = set(sa.inspect(bind).get_table_names())
        for key, _description in _PERMISSIONS:
            permission_id = _permission_id(bind, key)
            if permission_id is None:
                continue
            for table in _GRANT_TABLES:
                if table in table_names:
                    bind.execute(
                        sa.text(f"DELETE FROM {table} WHERE permission_id = :id"),
                        {"id": permission_id},
                    )
            bind.execute(
                sa.text("DELETE FROM permissions WHERE id = :id"), {"id": permission_id}
            )
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.execute(
        "DROP TRIGGER IF EXISTS custom_field_definition_lifecycle_guard ON custom_field_definitions"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_custom_field_definition_lifecycle()")
    op.drop_index(
        "ix_custom_field_values_definition_id", table_name="custom_field_values"
    )
    op.drop_index("ix_custom_field_values_target", table_name="custom_field_values")
    op.drop_index("ix_custom_field_values_tenant_id", table_name="custom_field_values")
    op.drop_table("custom_field_values")
    op.drop_index(
        "ix_custom_field_definitions_status", table_name="custom_field_definitions"
    )
    op.drop_index(
        "ix_custom_field_definitions_target_type", table_name="custom_field_definitions"
    )
    op.drop_index(
        "ix_custom_field_definitions_tenant_id", table_name="custom_field_definitions"
    )
    op.drop_table("custom_field_definitions")
