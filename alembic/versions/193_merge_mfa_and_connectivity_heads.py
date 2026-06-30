"""Merge MFA recovery codes with connectivity backup head.

Revision ID: 193_merge_mfa_and_connectivity_heads
Revises: 186_mfa_recovery_codes, 192_connectivity_state_backup
"""

from __future__ import annotations

revision = "193_merge_mfa_and_connectivity_heads"
down_revision = (
    "186_mfa_recovery_codes",
    "192_connectivity_state_backup",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
