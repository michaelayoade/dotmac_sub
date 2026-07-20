"""Merge the deployed legacy IP-assignment branch into the current chain.

Revision ID: 368_merge_legacy_ip_assignments_branch
Revises: 367_reports_support_permission, 153_ip_assignments_subscription_owner
"""

from __future__ import annotations

revision = "368_merge_legacy_ip_assignments_branch"
down_revision = (
    "367_reports_support_permission",
    "153_ip_assignments_subscription_owner",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
