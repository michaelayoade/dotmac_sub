"""Merge subscription permission and open PR remediation heads.

Revision ID: 195_merge_subscription_permission_and_open_pr_heads
Revises: 192_add_subscription_write_permission, 194_merge_open_pr_remediation_heads
"""

from __future__ import annotations

revision = "195_merge_subscription_permission_and_open_pr_heads"
down_revision = (
    "192_add_subscription_write_permission",
    "194_merge_open_pr_remediation_heads",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
