"""Cut device operation over to a binary, permanently verified lifecycle.

Revision ID: 416_binary_device_operational_lifecycle
Revises: 415_permanent_lifecycle_drainage

The migration backfills the rebuildable device projection to the public
``working``/``not_working`` vocabulary, retires observer switches that could
freeze verification, and re-enables the permanent verification inputs.

The projection is bounded by device inventory size. A five-second lock budget
and sixty-second statement budget fail closed on contention; retry through the
normal migration runner after resolving the blocking transaction.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "416_binary_device_operational_lifecycle"
down_revision = "415_permanent_lifecycle_drainage"
branch_labels = None
depends_on = None

_RETIRED_SETTINGS = (
    ("network_monitoring", "monitoring_coverage_enabled"),
    ("network_monitoring", "monitoring_inventory_sync_enabled"),
    ("network_monitoring", "channel_health_enabled"),
)

_PERMANENT_TASK_NAMES = (
    "app.tasks.monitoring_coverage.refresh_monitoring_coverage",
    "app.tasks.monitoring_cleanup.sync_inventory_to_monitoring",
    "app.tasks.channel_health.observe_channel_health",
)

_DELETE_SETTING = sa.text(
    "DELETE FROM domain_settings "
    "WHERE domain = CAST(:domain AS settingdomain) AND key = :key"
)


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    op.execute(
        sa.text(
            "UPDATE device_projections SET "
            "operational_status = CASE "
            "WHEN operational_status IN ('up', 'degraded', 'working') "
            "THEN 'working' ELSE 'not_working' END, "
            "operational_reason = CASE operational_reason "
            "WHEN 'not_warmed_retry_pending' THEN 'verification_not_started' "
            "WHEN 'never_seen_retry_pending' THEN 'verification_not_started' "
            "WHEN 'stale_retry_pending' THEN 'verification_expired' "
            "WHEN 'last_confirmed_online_retry_pending' "
            "THEN 'verification_expired' "
            "WHEN 'monitoring_unknown_retry_pending' "
            "THEN 'verification_inconclusive' "
            "WHEN 'indeterminate_retry_pending' "
            "THEN 'verification_inconclusive' "
            "WHEN 'no_path_retry_pending' "
            "THEN 'verification_path_unavailable' "
            "WHEN 'derivation_error_retry_pending' THEN 'verification_error' "
            "WHEN 'operational_state_not_available' "
            "THEN 'verification_not_configured' "
            "ELSE operational_reason END"
        )
    )
    op.alter_column(
        "device_projections",
        "operational_status",
        existing_type=sa.String(length=40),
        server_default="not_working",
        existing_nullable=False,
    )
    op.execute(
        sa.text(
            "DO $$ "
            "BEGIN "
            "IF NOT EXISTS ("
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = "
            "'ck_device_projection_binary_operational_status' "
            "AND conrelid = 'device_projections'::regclass"
            ") THEN "
            "ALTER TABLE device_projections "
            "ADD CONSTRAINT ck_device_projection_binary_operational_status "
            "CHECK (operational_status IN ('working', 'not_working')) NOT VALID; "
            "END IF; "
            "END "
            "$$;"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE device_projections "
            "VALIDATE CONSTRAINT ck_device_projection_binary_operational_status"
        )
    )

    for domain, key in _RETIRED_SETTINGS:
        op.execute(_DELETE_SETTING.bindparams(domain=domain, key=key))

    op.execute(
        sa.text(
            "UPDATE scheduled_tasks SET enabled = true, updated_at = now() "
            "WHERE task_name IN :task_names"
        ).bindparams(
            sa.bindparam("task_names", expanding=True, value=_PERMANENT_TASK_NAMES)
        )
    )


def downgrade() -> None:
    # Forward-only authority cutover. Restoring multi-state projections or
    # mutable verifier switches would recreate the drift mechanism.
    pass
