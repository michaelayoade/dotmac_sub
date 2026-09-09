"""Merge the independently developed 590 migration heads.

Revision ID: 591_merge_consolidated_open_pr_heads
Revises: 590_olt_observation_read_status,
    590_field_note_delivery_idempotency,
    590_field_expense_payment_permission
"""

from __future__ import annotations

revision = "591_merge_consolidated_open_pr_heads"
down_revision = (
    "590_olt_observation_read_status",
    "590_field_note_delivery_idempotency",
    "590_field_expense_payment_permission",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
