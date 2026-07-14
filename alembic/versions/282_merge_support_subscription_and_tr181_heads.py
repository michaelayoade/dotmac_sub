"""Merge support subscription controls and TR-181 DNS heads.

Revision ID: 282_merge_support_subscription_and_tr181_heads
Revises: 281_tr181_wan_observed_dns, 281_technical_support_subscription_activate
"""

from __future__ import annotations

revision = "282_merge_support_subscription_and_tr181_heads"
down_revision = (
    "281_tr181_wan_observed_dns",
    "281_technical_support_subscription_activate",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
