"""Add crontab scheduling to scheduled_tasks.

Lets a scheduled task run at a fixed wall-clock time (a 5-field cron expression)
instead of only a fixed interval — so ops can set *when* billing/dunning and
other jobs run, not just how often. Adds the ``crontab`` value to the
``scheduletype`` enum and a nullable ``cron_expr`` column (used only when
schedule_type == crontab).

Revision ID: 168_scheduled_task_crontab
Revises: 167_reseller_wht_and_consolidated_proofs
Create Date: 2026-06-22

Deploy note: the ``ALTER TYPE ... ADD VALUE`` must run as a role that owns the
enum type (e.g. ``postgres``), not the app role ``dotmac_app``. Both ops are
guarded (IF NOT EXISTS / column-presence) so re-runs are no-ops.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "168_scheduled_task_crontab"
down_revision = "167_reseller_wht_and_consolidated_proofs"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # SQLite (tests) derives the enum from the model, so the new member is
        # available without DDL.
        op.execute("ALTER TYPE scheduletype ADD VALUE IF NOT EXISTS 'crontab'")
    if not _has_column("scheduled_tasks", "cron_expr"):
        op.add_column(
            "scheduled_tasks",
            sa.Column("cron_expr", sa.String(length=120), nullable=True),
        )


def downgrade() -> None:
    if _has_column("scheduled_tasks", "cron_expr"):
        op.drop_column("scheduled_tasks", "cron_expr")
    # PostgreSQL cannot drop an enum value without a full type rebuild; leave the
    # 'crontab' value in place.
