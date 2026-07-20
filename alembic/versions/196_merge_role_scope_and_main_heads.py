"""Merge role scope cleanup and main migration heads.

Revision ID: 196_merge_role_scope_and_main_heads
Revises: 193_role_scope_cleanup_project_role, 195_merge_subscription_permission_and_open_pr_heads
"""

from __future__ import annotations

revision = "196_merge_role_scope_and_main_heads"
down_revision = (
    "193_role_scope_cleanup_project_role",
    "195_merge_subscription_permission_and_open_pr_heads",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
