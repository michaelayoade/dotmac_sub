"""Merge decoupling and compensation heads.

Revision ID: 035_merge_decoupling_heads
Revises: 028_preserve_profile_owner, 034_add_compensation_failures
Create Date: 2026-04-18

"""

revision = "035_merge_decoupling_heads"
down_revision = ("028_preserve_profile_owner", "034_add_compensation_failures")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
