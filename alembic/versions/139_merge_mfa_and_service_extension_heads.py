"""Merge MFA lockout and service extension migration heads.

Revision ID: 139_merge_mfa_and_service_extension_heads
Revises: 138_add_mfa_attempt_lockout, 138_add_service_extensions
Create Date: 2026-06-11
"""

from __future__ import annotations

revision = "139_merge_mfa_and_service_extension_heads"
down_revision = ("138_add_mfa_attempt_lockout", "138_add_service_extensions")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
