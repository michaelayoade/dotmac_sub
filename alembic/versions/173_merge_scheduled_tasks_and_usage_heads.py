"""Merge scheduled task and usage migration heads.

Revision ID: 173_merge_scheduled_tasks_and_usage_heads
Revises: 172_scheduled_tasks_unique_name, 172_splynx_usage_history_import
Create Date: 2026-06-24
"""

from __future__ import annotations

revision = "173_merge_scheduled_tasks_and_usage_heads"
down_revision = (
    "172_scheduled_tasks_unique_name",
    "172_splynx_usage_history_import",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
