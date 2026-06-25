"""Merge connectivity and usage migration heads.

Revision ID: 174_merge_connectivity_and_usage_heads
Revises: 173_merge_scheduled_tasks_and_usage_heads, 173_subscription_last_seen_framed_ip
Create Date: 2026-06-24
"""

from __future__ import annotations

revision = "174_merge_connectivity_and_usage_heads"
down_revision = (
    "173_merge_scheduled_tasks_and_usage_heads",
    "173_subscription_last_seen_framed_ip",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
