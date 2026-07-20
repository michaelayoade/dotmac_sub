"""Merge open PR remediation migration heads.

Revision ID: 194_merge_open_pr_remediation_heads
Revises: 193_merge_mfa_and_connectivity_heads, 193_merge_router_config_and_connectivity_heads, 193_merge_webhook_and_connectivity_heads
"""

from __future__ import annotations

revision = "194_merge_open_pr_remediation_heads"
down_revision = (
    "193_merge_mfa_and_connectivity_heads",
    "193_merge_router_config_and_connectivity_heads",
    "193_merge_webhook_and_connectivity_heads",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
