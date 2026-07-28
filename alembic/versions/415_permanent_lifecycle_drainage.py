"""Separate lifecycle drainage from mutable scheduler admission.

Revision ID: 415_permanent_lifecycle_drainage
Revises: 414_permanent_device_projection

Compensation retries, FUP reset, access projections, and accepted campaign work
must continue to converge after durable state exists. The former campaign
processing flag becomes an admission-only control: when the cutover gate is
closed, pre-cutover scheduled rows are moved to an explicit paused state before
the permanent runners are enabled.

The migration uses a five-second lock budget and a sixty-second statement
budget. Campaign selection uses the existing status/scheduled index; scheduled
task and setting updates are bounded control-plane writes. Retry through the
normal migration runner after resolving lock contention.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "415_permanent_lifecycle_drainage"
down_revision = "414_permanent_device_projection"
branch_labels = None
depends_on = None

_RETIRED_SETTINGS = (
    ("modules", "provisioning_compensation_retry"),
    ("provisioning", "compensation_retry_enabled"),
    ("usage", "device_login_sync_enabled"),
    ("scheduler", "broker_url"),
    ("scheduler", "result_backend"),
)

_PERMANENT_TASK_NAMES = (
    "app.tasks.provisioning.retry_pending_compensation_failures",
    "app.tasks.usage.lift_expired_fup_enforcement",
    "app.tasks.radius.reconcile_active_sessions",
    "app.tasks.radius_population.sync_device_login",
    "app.tasks.campaigns.process_due_campaigns",
    "app.tasks.campaigns.process_due_campaign_steps",
)

_DELETE_SETTING = sa.text(
    "DELETE FROM domain_settings "
    "WHERE domain = CAST(:domain AS settingdomain) AND key = :key"
)


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    # A missing gate has always meant disabled. Pause only work that has not
    # started; `sending` campaigns and accepted nurture sequences keep draining.
    op.execute(
        sa.text(
            "UPDATE campaigns SET status = 'paused', updated_at = now(), "
            "metadata = (COALESCE(metadata::jsonb, '{}'::jsonb) || "
            "jsonb_build_object("
            "'processing_deferred_reason', 'campaign_admission_cutover_disabled', "
            "'processing_deferred_at', now()"
            "))::json "
            "WHERE status = 'scheduled' AND NOT EXISTS ("
            "SELECT 1 FROM domain_settings "
            "WHERE domain = CAST('comms' AS settingdomain) "
            "AND key = 'campaign_processing_enabled' AND is_active = true "
            "AND replace(lower(COALESCE(value_json::text, value_text, '')), "
            "'\"', '') IN ('1', 'true', 'yes', 'on')"
            ")"
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
    # Forward-only authority cutover. Restoring task switches or silently
    # rescheduling paused campaigns would recreate the drift mechanism.
    pass
