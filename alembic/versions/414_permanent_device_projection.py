"""Make unified device-projection repair permanent.

Revision ID: 414_permanent_device_projection
Revises: 413_audit_actor_label

``device_projections`` is a rebuildable read model of canonical network state.
Its repair owner must continue to converge after source changes, so a mutable
setting or disabled scheduled-task row cannot stop reconciliation.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "414_permanent_device_projection"
down_revision = "413_audit_actor_label"
branch_labels = None
depends_on = None

_SCHEDULE_NAME = "device_projection_reconcile"
_TASK_NAME = "app.tasks.device_projection.reconcile_device_projections"
_RETIRED_SETTING = (
    "network_monitoring",
    "device_projection_reconcile_enabled",
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM domain_settings "
            "WHERE domain = CAST(:domain AS settingdomain) AND key = :key"
        ).bindparams(
            domain=_RETIRED_SETTING[0],
            key=_RETIRED_SETTING[1],
        )
    )
    op.execute(
        sa.text(
            "UPDATE scheduled_tasks SET enabled = true, updated_at = now() "
            "WHERE name = :name OR task_name = :task_name"
        ).bindparams(name=_SCHEDULE_NAME, task_name=_TASK_NAME)
    )


def downgrade() -> None:
    # Forward-only authority cutover. Recreating the setting would restore a
    # parallel decision path and could freeze projection repair.
    pass
