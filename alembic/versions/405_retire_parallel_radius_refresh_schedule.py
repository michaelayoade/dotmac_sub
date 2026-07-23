"""Retire the parallel periodic RADIUS projection refresh.

Revision ID: 405_retire_parallel_radius_refresh
Revises: 404_team_inbox_sot_completion

The permanent account-access reconciler owns periodic drift detection. The
full RADIUS projection writer remains available for event-time and reconciler-
requested delivery, but it must not have an independent feature/settings gate
or ScheduledTask row.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "405_retire_parallel_radius_refresh"
down_revision = "404_team_inbox_sot_completion"
branch_labels = None
depends_on = None

_REFRESH_TASK = "app.tasks.radius_population.refresh_radius_from_subs"
_RETIRED_SETTINGS = (
    ("subscriber", "radius_refresh_safety_net_enabled"),
    ("subscriber", "radius_refresh_safety_net_interval_minutes"),
)


def upgrade() -> None:
    for domain, key in _RETIRED_SETTINGS:
        op.execute(
            sa.text(
                "DELETE FROM domain_settings "
                "WHERE domain = CAST(:domain AS settingdomain) AND key = :key"
            ).bindparams(domain=domain, key=key)
        )
    op.execute(
        sa.text(
            "UPDATE scheduled_tasks SET enabled = false, updated_at = now() "
            "WHERE task_name = :task_name"
        ).bindparams(task_name=_REFRESH_TASK)
    )


def downgrade() -> None:
    # Forward-only authority cutover: the parallel repair schedule and its
    # retired decision inputs must not be recreated.
    pass
