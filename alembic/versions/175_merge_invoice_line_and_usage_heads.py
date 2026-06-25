"""Merge invoice line idempotency and usage migration heads.

Revision ID: 175_merge_invoice_line_and_usage_heads
Revises: 174_invoice_line_subscription_idempotency, 174_merge_connectivity_and_usage_heads
Create Date: 2026-06-25
"""

from __future__ import annotations

revision = "175_merge_invoice_line_and_usage_heads"
down_revision = (
    "174_invoice_line_subscription_idempotency",
    "174_merge_connectivity_and_usage_heads",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
