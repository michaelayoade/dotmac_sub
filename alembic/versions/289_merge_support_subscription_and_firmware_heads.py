"""Merge support subscription controls and firmware catalog heads.

Revision ID: 289_merge_support_subscription_and_firmware_heads
Revises: 282_merge_support_subscription_and_tr181_heads, 288_olt_firmware_operation_type
"""

from __future__ import annotations

revision = "289_merge_support_subscription_and_firmware_heads"
down_revision = (
    "282_merge_support_subscription_and_tr181_heads",
    "288_olt_firmware_operation_type",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
