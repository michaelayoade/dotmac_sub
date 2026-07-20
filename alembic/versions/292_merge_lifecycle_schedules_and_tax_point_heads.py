"""Merge subscription lifecycle schedule and credit-note tax point heads.

Revision ID: 292_merge_lifecycle_schedules_and_tax_point_heads
Revises: 290_subscription_lifecycle_schedules, 291_credit_note_tax_point
"""

from __future__ import annotations

revision = "292_merge_lifecycle_schedules_and_tax_point_heads"
down_revision = (
    "290_subscription_lifecycle_schedules",
    "291_credit_note_tax_point",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
