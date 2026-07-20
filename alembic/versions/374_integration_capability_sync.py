"""Bind integration sync jobs and runs to versioned capabilities.

Revision ID: 374_integration_capability_sync
Revises: 373_integration_platform_foundation
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "374_integration_capability_sync"
down_revision = "373_integration_platform_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integration_jobs",
        sa.Column(
            "capability_binding_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_integration_jobs_capability_binding",
        "integration_jobs",
        "integration_capability_bindings",
        ["capability_binding_id"],
        ["id"],
    )
    # Existing unbound jobs are historical records only. They are explicitly
    # disabled at cutover; operators create/validate an installation and bind a
    # new job instead of retaining an adapter/action execution path.
    op.execute(
        "UPDATE integration_jobs SET is_active = false "
        "WHERE capability_binding_id IS NULL"
    )
    op.create_check_constraint(
        "ck_integration_jobs_active_binding",
        "integration_jobs",
        "NOT is_active OR capability_binding_id IS NOT NULL",
    )
    op.create_index(
        "ix_integration_jobs_capability_active",
        "integration_jobs",
        ["capability_binding_id", "is_active"],
    )
    op.drop_column("integration_jobs", "adapter_key")
    op.drop_column("integration_jobs", "action")
    op.drop_constraint(
        "integration_targets_connector_config_id_fkey",
        "integration_targets",
        type_="foreignkey",
    )
    op.drop_column("integration_targets", "connector_config_id")

    for column_name, target_table in (
        ("installation_id", "integration_installations"),
        ("capability_binding_id", "integration_capability_bindings"),
        ("config_revision_id", "integration_config_revisions"),
    ):
        op.add_column(
            "integration_runs",
            sa.Column(column_name, postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_integration_runs_{column_name}",
            "integration_runs",
            target_table,
            [column_name],
            ["id"],
        )
    for column_name, length in (
        ("capability_id", 160),
        ("connector_key", 120),
        ("connector_version", 32),
        ("manifest_digest", 64),
    ):
        op.add_column(
            "integration_runs",
            sa.Column(column_name, sa.String(length=length), nullable=True),
        )
    op.create_index(
        "ix_integration_runs_binding_started",
        "integration_runs",
        ["capability_binding_id", "started_at"],
    )

    op.create_table(
        "integration_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "capability_binding_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("cursor_json", sa.JSON(), nullable=False),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("advanced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_integration_checkpoints_version"),
        sa.ForeignKeyConstraint(
            ["capability_binding_id"],
            ["integration_capability_bindings.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["integration_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"], ["integration_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "capability_binding_id",
            name="uq_integration_checkpoints_job_binding",
        ),
    )


def downgrade() -> None:
    op.drop_table("integration_checkpoints")
    op.drop_index("ix_integration_runs_binding_started", table_name="integration_runs")
    for column_name in (
        "config_revision_id",
        "capability_binding_id",
        "installation_id",
    ):
        op.drop_constraint(
            f"fk_integration_runs_{column_name}",
            "integration_runs",
            type_="foreignkey",
        )
    for column_name in (
        "manifest_digest",
        "connector_version",
        "connector_key",
        "capability_id",
        "config_revision_id",
        "capability_binding_id",
        "installation_id",
    ):
        op.drop_column("integration_runs", column_name)
    op.add_column(
        "integration_targets",
        sa.Column("connector_config_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "integration_targets_connector_config_id_fkey",
        "integration_targets",
        "connector_configs",
        ["connector_config_id"],
        ["id"],
    )
    op.add_column(
        "integration_jobs", sa.Column("action", sa.String(length=80), nullable=True)
    )
    op.add_column(
        "integration_jobs",
        sa.Column("adapter_key", sa.String(length=80), nullable=True),
    )
    op.drop_index(
        "ix_integration_jobs_capability_active", table_name="integration_jobs"
    )
    op.drop_constraint(
        "ck_integration_jobs_active_binding", "integration_jobs", type_="check"
    )
    op.drop_constraint(
        "fk_integration_jobs_capability_binding",
        "integration_jobs",
        type_="foreignkey",
    )
    op.drop_column("integration_jobs", "capability_binding_id")
