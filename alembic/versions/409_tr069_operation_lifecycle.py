"""Cut TR-069 jobs over to durable network-operation dispatch.

Revision ID: 409_tr069_operation_lifecycle
Revises: 408_radius_session_latest_projection

The job table becomes an operator-facing projection linked one-to-one with the
canonical network-operation ledger. New command payloads use an encrypted-at-
rest column. Pre-cutover executable rows are terminalized; no legacy command
path remains after this migration.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "409_tr069_operation_lifecycle"
down_revision = "408_radius_session_latest_projection"
branch_labels = None
depends_on = None

_OLD_RECONCILER_TASK = "app.tasks.tr069.execute_pending_jobs"
_RECONCILER_TASK = "app.tasks.tr069.reconcile_command_outcomes"
_OLD_SCHEDULE_NAME = "tr069_job_executor"
_RECONCILER_SCHEDULE_NAME = "tr069_command_reconciler"
_RETIRED_SETTING = ("network", "tr069_job_execution_enabled")
_OLD_INTERVAL_KEY = "tr069_job_execution_interval_seconds"
_RECONCILIATION_INTERVAL_KEY = "tr069_command_reconciliation_interval_seconds"
_OLD_CONTROL_KEY = "network_tr069_job_execution"
_NEW_CONTROL_KEY = "network_tr069_command_admission"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE networkoperationtype "
                "ADD VALUE IF NOT EXISTS 'cpe_tr069_command'"
            )
            op.execute("ALTER TYPE tr069jobstatus ADD VALUE IF NOT EXISTS 'unverified'")

    op.add_column("tr069_jobs", sa.Column("secure_payload", sa.Text(), nullable=True))
    op.add_column(
        "tr069_jobs",
        sa.Column("network_operation_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "tr069_jobs",
        sa.Column("external_task_ids", sa.JSON(), nullable=True),
    )
    op.add_column(
        "tr069_jobs",
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tr069_jobs",
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tr069_jobs_network_operation_id",
        "tr069_jobs",
        "network_operations",
        ["network_operation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_tr069_jobs_network_operation_id",
        "tr069_jobs",
        ["network_operation_id"],
    )
    # The old setting controlled drain/retry after durable admission. It is
    # retired; the executor is a permanent lifecycle responsibility.
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = CAST(:domain AS settingdomain) AND key = :key"
        ).bindparams(domain=_RETIRED_SETTING[0], key=_RETIRED_SETTING[1])
    )
    # Preserve the operator's current flag value under the new, precise
    # admission-only identity, then remove the old executable control.
    op.execute(
        sa.text(
            "UPDATE domain_settings SET key = :new_key, updated_at = now() "
            "WHERE domain = CAST('modules' AS settingdomain) AND key = :old_key "
            "AND NOT EXISTS ("
            "SELECT 1 FROM domain_settings existing "
            "WHERE existing.domain = CAST('modules' AS settingdomain) "
            "AND existing.key = :new_key)"
        ).bindparams(old_key=_OLD_CONTROL_KEY, new_key=_NEW_CONTROL_KEY)
    )
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = CAST('modules' AS settingdomain) AND key = :old_key"
        ).bindparams(old_key=_OLD_CONTROL_KEY)
    )
    op.execute(
        sa.text(
            "UPDATE domain_settings SET key = :new_key, updated_at = now() "
            "WHERE domain = CAST('network' AS settingdomain) AND key = :old_key "
            "AND NOT EXISTS ("
            "SELECT 1 FROM domain_settings existing "
            "WHERE existing.domain = CAST('network' AS settingdomain) "
            "AND existing.key = :new_key)"
        ).bindparams(
            old_key=_OLD_INTERVAL_KEY,
            new_key=_RECONCILIATION_INTERVAL_KEY,
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = CAST('network' AS settingdomain) AND key = :old_key"
        ).bindparams(old_key=_OLD_INTERVAL_KEY)
    )
    op.execute(
        sa.text(
            "DELETE FROM scheduled_tasks legacy "
            "WHERE (legacy.task_name = :old_task OR legacy.name = :old_name) "
            "AND EXISTS ("
            "SELECT 1 FROM scheduled_tasks current "
            "WHERE current.id <> legacy.id "
            "AND (current.task_name = :new_task OR current.name = :new_name))"
        ).bindparams(
            new_name=_RECONCILER_SCHEDULE_NAME,
            new_task=_RECONCILER_TASK,
            old_task=_OLD_RECONCILER_TASK,
            old_name=_OLD_SCHEDULE_NAME,
        )
    )
    op.execute(
        sa.text(
            "UPDATE scheduled_tasks SET name = :new_name, "
            "task_name = :new_task, enabled = true, updated_at = now() "
            "WHERE task_name IN (:old_task, :new_task) "
            "OR name IN (:old_name, :new_name)"
        ).bindparams(
            new_name=_RECONCILER_SCHEDULE_NAME,
            new_task=_RECONCILER_TASK,
            old_task=_OLD_RECONCILER_TASK,
            old_name=_OLD_SCHEDULE_NAME,
        )
    )

    # No pre-cutover command can prove it owns a durable operation/dispatch
    # claim. Queued rows are failed without execution; in-flight rows are
    # unverified because the ACS side effect may already have happened.
    op.execute(
        sa.text(
            "UPDATE tr069_jobs SET status = 'failed', "
            "error = 'Pre-cutover queued command retired without execution.', "
            "completed_at = now(), last_observed_at = now(), payload = NULL "
            "WHERE network_operation_id IS NULL AND status = 'queued'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tr069_jobs SET status = 'unverified', "
            "error = 'Pre-cutover command had no durable execution claim; "
            "review current device state before retrying.', "
            "completed_at = now(), last_observed_at = now(), payload = NULL "
            "WHERE network_operation_id IS NULL "
            "AND status IN ('running', 'pending')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tr069_jobs SET payload = NULL WHERE network_operation_id IS NULL"
        )
    )
    op.drop_column("tr069_jobs", "max_retries")
    op.drop_column("tr069_jobs", "retry_count")


def downgrade() -> None:
    # Forward-only authority cutover. Operation history, enum values, encrypted
    # payloads, and permanent-drain semantics are intentionally retained.
    pass
