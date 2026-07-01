"""Merge webhook delivery controls with connectivity backup head.

Revision ID: 193_merge_webhook_and_connectivity_heads
Revises: 187_webhook_endpoint_delivery_controls, 192_connectivity_state_backup
"""

from __future__ import annotations

revision = "193_merge_webhook_and_connectivity_heads"
down_revision = (
    "187_webhook_endpoint_delivery_controls",
    "192_connectivity_state_backup",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
