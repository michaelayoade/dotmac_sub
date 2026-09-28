"""Add the native Automation Center script control plane.

Revision ID: 626_automation_script_control_plane
Revises: 625_automation_run_retry_audit
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "626_automation_script_control_plane"
down_revision: str | None = "625_automation_run_retry_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("automation:script:read", "View Automation Center scripts"),
    ("automation:script:create", "Create Automation Center script drafts"),
    ("automation:script:update", "Edit Automation Center script drafts"),
    ("automation:script:publish", "Publish Automation Center scripts"),
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


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation
          ON {table}
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
        "automation_scripts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("language", sa.String(length=24), nullable=False),
        sa.Column("target_type", sa.String(length=120), nullable=False),
        sa.Column("event_name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("active_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
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
            "kind IN ('client_script', 'server_script')",
            name="ck_automation_scripts_kind",
        ),
        sa.CheckConstraint(
            "language IN ('javascript')",
            name="ck_automation_scripts_language",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'paused', 'retired')",
            name="ck_automation_scripts_status",
        ),
        sa.CheckConstraint("length(trim(key)) > 0", name="ck_automation_scripts_key"),
        sa.CheckConstraint(
            "length(trim(target_type)) > 0 AND length(trim(event_name)) > 0",
            name="ck_automation_scripts_target_event",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_automation_scripts_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "key", name="uq_automation_scripts_tenant_key"
        ),
    )
    op.create_index(
        "ix_automation_scripts_target_type", "automation_scripts", ["target_type"]
    )
    op.create_index(
        "ix_automation_scripts_event_name", "automation_scripts", ["event_name"]
    )
    _tenant_policy("automation_scripts")

    op.create_table(
        "automation_script_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("script_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("api_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("limits", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_by", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "version >= 1", name="ck_automation_script_versions_version"
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64", name="ck_automation_script_versions_hash"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "script_id"],
            ["automation_scripts.tenant_id", "automation_scripts.id"],
            ondelete="CASCADE",
            name="fk_automation_script_versions_script_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_automation_script_versions_tenant_id"
        ),
        sa.UniqueConstraint(
            "script_id", "version", name="uq_automation_script_versions_revision"
        ),
    )
    op.create_index(
        "uq_automation_script_versions_draft",
        "automation_script_versions",
        ["script_id"],
        unique=True,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_foreign_key(
        "fk_automation_scripts_active_version_tenant",
        "automation_scripts",
        "automation_script_versions",
        ["tenant_id", "active_version_id"],
        ["tenant_id", "id"],
    )
    _tenant_policy("automation_script_versions")

    op.create_table(
        "automation_script_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("script_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("script_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(length=120), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_code", sa.String(length=160), nullable=True),
        sa.Column("error_code", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'blocked', 'reconciliation_required')",
            name="ck_automation_script_runs_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "script_id"],
            ["automation_scripts.tenant_id", "automation_scripts.id"],
            name="fk_automation_script_runs_script_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "script_version_id"],
            ["automation_script_versions.tenant_id", "automation_script_versions.id"],
            name="fk_automation_script_runs_version_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_automation_script_runs_tenant_id"
        ),
        sa.UniqueConstraint(
            "script_version_id", "event_id", name="uq_automation_script_runs_event"
        ),
    )
    op.create_index(
        "ix_automation_script_runs_script_id", "automation_script_runs", ["script_id"]
    )
    _tenant_policy("automation_script_runs")
    _seed_permissions(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    if "permissions" in set(sa.inspect(bind).get_table_names()):
        for key, _description in _PERMISSIONS:
            permission_id = _permission_id(bind, key)
            if permission_id is None:
                continue
            for table in _GRANT_TABLES:
                if table in set(sa.inspect(bind).get_table_names()):
                    bind.execute(
                        sa.text(f"DELETE FROM {table} WHERE permission_id = :id"),
                        {"id": permission_id},
                    )
            bind.execute(
                sa.text("DELETE FROM permissions WHERE id = :id"),
                {"id": permission_id},
            )
    for table in (
        "automation_script_runs",
        "automation_script_versions",
        "automation_scripts",
    ):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_index(
        "ix_automation_script_runs_script_id", table_name="automation_script_runs"
    )
    op.drop_table("automation_script_runs")
    op.drop_constraint(
        "fk_automation_scripts_active_version_tenant",
        "automation_scripts",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_automation_script_versions_draft", table_name="automation_script_versions"
    )
    op.drop_table("automation_script_versions")
    op.drop_index("ix_automation_scripts_event_name", table_name="automation_scripts")
    op.drop_index("ix_automation_scripts_target_type", table_name="automation_scripts")
    op.drop_table("automation_scripts")
