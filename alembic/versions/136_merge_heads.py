"""Merge the forked 134 heads (user-invite expiry / service requests).

Two sessions branched migration 134 off 133 in parallel; this empty merge
revision reunifies the chain so plain ``alembic upgrade head`` works again.

Revision ID: 136_merge_heads
Revises: 134_extend_user_invite_expiry, 135_add_payment_proofs
Create Date: 2026-06-10
"""

from __future__ import annotations

revision = "136_merge_heads"
down_revision = ("134_extend_user_invite_expiry", "135_add_payment_proofs")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
