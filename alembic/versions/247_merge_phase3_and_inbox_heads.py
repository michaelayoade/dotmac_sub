"""Merge Phase 3 and inbox migration heads.

Revision ID: 247_merge_phase3_inbox_heads
Revises: 244_phase3_expand_b_tables, 246_inbox_complete_ops
Create Date: 2026-07-10
"""

from __future__ import annotations

revision = "247_merge_phase3_inbox_heads"
down_revision = ("244_phase3_expand_b_tables", "246_inbox_complete_ops")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
