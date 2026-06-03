"""Add (is_active, status) composite index on notifications.

The admin sidebar's `_count_unread_notifications` runs
`COUNT(*) WHERE is_active = TRUE AND status IN ('queued', 'sending')` on every
admin page load (modulo cache). With ~9k+ queued rows and no supporting index,
that's a full table scan that dominates layout latency on a remote DB.

Revision ID: 112_add_notifications_is_active_status_index
Revises: 111_add_automation_rule_observability
Create Date: 2026-05-26
"""

from __future__ import annotations

from alembic import op

revision = "112_add_notifications_is_active_status_index"
down_revision = "111_add_automation_rule_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_is_active_status "
        "ON notifications (is_active, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_notifications_is_active_status")
